"""aiosqlite-состояние бота. Один широкий postings-row на отправление —
жизненный цикл моделируется nullable timestamp-колонками (см. план).
"""
import json

import aiosqlite

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
    posting_number   TEXT PRIMARY KEY,
    shop_key         TEXT NOT NULL,
    status           TEXT NOT NULL,
    products_json    TEXT NOT NULL,
    created_at_ozon  TEXT,
    first_seen_at    INTEGER NOT NULL,
    notified_at      INTEGER,
    label_ready_at   INTEGER,
    label_sent_at    INTEGER,
    label_pdf_cached BLOB,
    printed_at       INTEGER,
    cancelled_at     INTEGER,
    cancel_notified_at INTEGER,
    last_checked_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_postings_notify ON postings (notified_at);
CREATE INDEX IF NOT EXISTS idx_postings_label ON postings (status, label_sent_at);
CREATE INDEX IF NOT EXISTS idx_postings_printed ON postings (printed_at);
CREATE INDEX IF NOT EXISTS idx_postings_shop ON postings (shop_key);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# posting_number — это идентификатор заказа Ozon, общий для всей площадки
# (не переиспользуется другими продавцами), поэтому он остаётся единственным
# первичным ключом даже при нескольких магазинах в одной БД. shop_key нужен
# только чтобы знать, чьими ключами API забирать/обновлять конкретное
# отправление, и чтобы подписывать сообщения в Telegram.

# "cancelled" подтверждён вживую (2026-07, оба магазина). "client_arbitration"
# оставлен как разумное предположение по названию — не встречался в реальных
# данных, при случае стоит свериться отдельно.
CANCELLED_STATUSES = {"cancelled", "client_arbitration"}


async def init_db():
    db = await aiosqlite.connect(config.DB_PATH)
    await db.execute("PRAGMA journal_mode=WAL;")
    await db.execute("PRAGMA synchronous=NORMAL;")
    await db.executescript(SCHEMA)
    await db.commit()
    return db


# ----------------------------------------------------------------- kv ----

async def kv_get(db, lock, key, default=None):
    async with lock:
        cur = await db.execute("SELECT value FROM kv WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default


async def kv_set(db, lock, key, value):
    async with lock:
        await db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        await db.commit()


# ------------------------------------------------------------ postings ----

async def upsert_posting(db, lock, shop_key, posting_number, status, products, created_at_ozon, now):
    """Вставляет новое отправление (notified_at=NULL) либо обновляет статус
    существующего. Отмену обрабатывает отдельно вызывающий код (см.
    mark_cancelled_if_needed). notified_at/label_sent_at/printed_at никогда
    не трогаются в ветке UPDATE — это и гарантирует, что уже отправленное
    боту в Telegram отправление не пришлётся повторно."""
    async with lock:
        cur = await db.execute(
            "SELECT status FROM postings WHERE posting_number = ?", (posting_number,)
        )
        row = await cur.fetchone()
        products_json = json.dumps(products, ensure_ascii=False)
        if row is None:
            await db.execute(
                "INSERT INTO postings "
                "(posting_number, shop_key, status, products_json, created_at_ozon, "
                " first_seen_at, last_checked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (posting_number, shop_key, status, products_json, created_at_ozon, now, now),
            )
        else:
            await db.execute(
                "UPDATE postings SET status = ?, products_json = ?, last_checked_at = ? "
                "WHERE posting_number = ?",
                (status, products_json, now, posting_number),
            )
        await db.commit()
        return row is None  # True, если это новое отправление


async def mark_cancelled_if_needed(db, lock, posting_number, status, now):
    if status not in CANCELLED_STATUSES:
        return False
    async with lock:
        await db.execute(
            "UPDATE postings SET cancelled_at = ? "
            "WHERE posting_number = ? AND cancelled_at IS NULL",
            (now, posting_number),
        )
        await db.commit()
        return True


async def get_active_tracked_numbers(db, lock, shop_key):
    """posting_number конкретного магазина, за которыми ещё нужно следить
    (этикетка не отправлена, отмена ещё не зафиксирована) — используется,
    чтобы заметить отправления, пропавшие из основной выборки по статусам
    (значит статус сменился, в т.ч. возможно на 'отменено'). Скопировано по
    shop_key, потому что уточняющий запрос идёт под ключами этого магазина."""
    async with lock:
        cur = await db.execute(
            "SELECT posting_number FROM postings "
            "WHERE shop_key = ? AND label_sent_at IS NULL AND cancelled_at IS NULL",
            (shop_key,),
        )
        return [row[0] for row in await cur.fetchall()]


async def get_cancel_unnotified(db, lock):
    async with lock:
        cur = await db.execute(
            "SELECT posting_number, shop_key, products_json FROM postings "
            "WHERE cancelled_at IS NOT NULL AND cancel_notified_at IS NULL"
        )
        return await cur.fetchall()


async def mark_cancel_notified(db, lock, posting_number, now):
    async with lock:
        await db.execute(
            "UPDATE postings SET cancel_notified_at = ? WHERE posting_number = ?",
            (now, posting_number),
        )
        await db.commit()


async def get_unnotified(db, lock):
    async with lock:
        cur = await db.execute(
            "SELECT posting_number, shop_key, status, products_json FROM postings "
            "WHERE notified_at IS NULL AND cancelled_at IS NULL "
            "ORDER BY first_seen_at ASC"
        )
        return await cur.fetchall()


async def mark_notified(db, lock, posting_number, now):
    async with lock:
        await db.execute(
            "UPDATE postings SET notified_at = ? WHERE posting_number = ?",
            (now, posting_number),
        )
        await db.commit()


async def get_label_pending(db, lock):
    """Отправления в awaiting_deliver, для которых этикетка ещё не отправлена
    (по всем магазинам сразу — вызывающий код группирует по shop_key, чтобы
    забирать этикетку под ключами нужного магазина)."""
    async with lock:
        cur = await db.execute(
            "SELECT posting_number, shop_key, status, products_json FROM postings "
            "WHERE status = 'awaiting_deliver' AND label_sent_at IS NULL "
            "AND cancelled_at IS NULL ORDER BY first_seen_at ASC"
        )
        return await cur.fetchall()


async def mark_label_sent(db, lock, posting_number, pdf_bytes, now):
    async with lock:
        await db.execute(
            "UPDATE postings SET label_sent_at = ?, label_pdf_cached = ?, "
            "label_ready_at = COALESCE(label_ready_at, ?) WHERE posting_number = ?",
            (now, pdf_bytes, now, posting_number),
        )
        await db.commit()


async def get_pending_for_merge(db, lock, label_required=True):
    """Кандидаты для /merge — ещё не напечатанные и не отменённые, из ЛЮБОГО
    магазина (печать/сборка PDF идёт из кэша, ключи API для этого не нужны)."""
    async with lock:
        clause = "AND label_sent_at IS NOT NULL " if label_required else ""
        cur = await db.execute(
            "SELECT posting_number, shop_key, status, products_json, label_sent_at FROM postings "
            "WHERE printed_at IS NULL AND cancelled_at IS NULL " + clause +
            "ORDER BY first_seen_at ASC"
        )
        return await cur.fetchall()


async def get_postings_by_numbers(db, lock, posting_numbers):
    async with lock:
        placeholders = ",".join("?" for _ in posting_numbers)
        cur = await db.execute(
            f"SELECT posting_number, shop_key, status, products_json, label_pdf_cached, "
            f"printed_at, cancelled_at FROM postings "
            f"WHERE posting_number IN ({placeholders})",
            list(posting_numbers),
        )
        return await cur.fetchall()


async def mark_printed(db, lock, posting_numbers, now):
    if not posting_numbers:
        return
    async with lock:
        placeholders = ",".join("?" for _ in posting_numbers)
        await db.execute(
            f"UPDATE postings SET printed_at = ? WHERE posting_number IN ({placeholders})",
            [now] + list(posting_numbers),
        )
        await db.commit()


async def get_history(db, lock, limit=30):
    async with lock:
        cur = await db.execute(
            "SELECT posting_number, shop_key, products_json, printed_at FROM postings "
            "WHERE printed_at IS NOT NULL ORDER BY printed_at DESC LIMIT ?",
            (limit,),
        )
        return await cur.fetchall()


async def get_stats(db, lock):
    async with lock:
        cur = await db.execute(
            "SELECT "
            " COUNT(*), "
            " SUM(CASE WHEN printed_at IS NULL AND cancelled_at IS NULL THEN 1 ELSE 0 END), "
            " SUM(CASE WHEN cancelled_at IS NOT NULL THEN 1 ELSE 0 END), "
            " SUM(CASE WHEN printed_at IS NOT NULL THEN 1 ELSE 0 END) "
            "FROM postings"
        )
        total, pending, cancelled, printed = await cur.fetchone()
        return {
            "total": total or 0,
            "pending": pending or 0,
            "cancelled": cancelled or 0,
            "printed": printed or 0,
        }


async def get_stats_by_shop(db, lock):
    async with lock:
        cur = await db.execute(
            "SELECT shop_key, COUNT(*), "
            " SUM(CASE WHEN printed_at IS NULL AND cancelled_at IS NULL THEN 1 ELSE 0 END), "
            " SUM(CASE WHEN printed_at IS NOT NULL THEN 1 ELSE 0 END) "
            "FROM postings GROUP BY shop_key"
        )
        rows = await cur.fetchall()
        return {
            shop_key: {"total": total or 0, "pending": pending or 0, "printed": printed or 0}
            for shop_key, total, pending, printed in rows
        }
