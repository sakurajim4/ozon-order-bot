"""Оркестрация: поллинг Ozon (Проход A/B из плана) по НЕСКОЛЬКИМ магазинам,
long-polling Telegram, команды/инлайн-кнопки для сборки PDF, авто-рестарт.

Команды и кнопки реагируют на любой chat_id из config.ADMIN_CHAT_IDS
(CHAT_ID + config.EXTRA_CHAT_IDS, см. .env.example) — несколько человек
могут независимо друг от друга получать уведомления и управлять ботом
(каждый свою /merge-сессию, см. merge_sessions, keyed по chat_id).
"""
import asyncio
import datetime
import json
import time

import httpx

import config
import db as db_module
import ozon_client
import pdf_label
import webapp_server  # безопасен к циклическому импорту: webapp_server трогает bot.* только внутри функций, не на уровне модуля

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MERGE_PAGE_SIZE = 8

# Постоянная клавиатура снизу — замена самым частым командам (кроме /stats
# и /ping, им кнопки не нужны). Нажатие такой кнопки прилетает боту обычным
# текстовым сообщением с этой же надписью — см. BUTTON_TEXT_TO_COMMAND.
MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "📋 Ожидают"}, {"text": "🖨 Печать"}],
        [{"text": "🔁 Повтор печати"}, {"text": "📜 История"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}
if config.WEBAPP_PUBLIC_URL:
    # Mini App (webapp_server.py) — пусто по умолчанию и не задано в .env на
    # VPS, так что на проде эта кнопка не появляется, пока не решат явно
    # выкатывать (см. план разработки мини-аппа).
    MAIN_KEYBOARD["keyboard"].append(
        [{"text": "🗂 Список заказов", "web_app": {"url": config.WEBAPP_PUBLIC_URL}}]
    )

BUTTON_TEXT_TO_COMMAND = {
    "📋 Ожидают": "/pending",
    "🖨 Печать": "/merge",
    "🔁 Повтор печати": "/reprint",
    "📜 История": "/history",
}

SHOPS_BY_KEY = {shop.key: shop for shop in config.SHOPS}


def _shop_name(shop_key: str) -> str:
    shop = SHOPS_BY_KEY.get(shop_key)
    return shop.name if shop else f"магазин {shop_key}"


# ============================================================
# === Telegram: низкоуровневые вызовы
# ============================================================

def _telegram_client(timeout):
    """httpx-клиент для api.telegram.org — единственное место, где включается
    config.TELEGRAM_PROXY (Ozon-запросы и скачивание фото с CDN Ozon через
    него НЕ идут)."""
    return httpx.AsyncClient(timeout=timeout, proxy=config.TELEGRAM_PROXY)


async def telegram_send(bot_token, chat_id, text, reply_markup=None) -> bool:
    """Возвращает True, только если ВСЕ чанки текста реально ушли — вызовы,
    для которых важна exactly-once семантика (уведомления/отмены в
    poll_once), должны проверять это перед тем, как помечать что-то
    отправленным в БД, а не полагаться на отсутствие исключения (раньше
    ошибка отправки тут просто логировалась и терялась, и заказ мог
    оказаться помечен notified_at, хотя сообщение не дошло)."""
    api = TELEGRAM_API.format(token=bot_token, method="sendMessage")
    ok = True
    async with _telegram_client(25) as client:
        for start in range(0, max(1, len(text)), 4000):
            chunk = text[start:start + 4000]
            payload = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}
            if reply_markup is not None:
                payload["reply_markup"] = json.dumps(reply_markup)
            try:
                r = await client.post(api, data=payload)
                r.raise_for_status()
            except Exception as e:
                print(f"[telegram] send failed: {type(e).__name__}: {e}")
                ok = False
    return ok


async def telegram_send_keyboard(bot_token, chat_id, text, reply_markup) -> int:
    api = TELEGRAM_API.format(token=bot_token, method="sendMessage")
    payload = {"chat_id": chat_id, "text": text, "reply_markup": json.dumps(reply_markup)}
    async with _telegram_client(25) as client:
        r = await client.post(api, data=payload)
        r.raise_for_status()
        return r.json()["result"]["message_id"]


async def telegram_edit_message(bot_token, chat_id, message_id, text, reply_markup=None):
    """Меняет и текст, и инлайн-клавиатуру сообщения за один вызов — нужно,
    чтобы счётчик "выбрано N из M" в тексте обновлялся вместе с галочками."""
    api = TELEGRAM_API.format(token=bot_token, method="editMessageText")
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    async with _telegram_client(25) as client:
        r = await client.post(api, data=payload)
        if r.status_code != 200 and "not modified" not in r.text:
            print(f"[telegram] edit message failed: {r.status_code} {r.text[:200]}")


async def telegram_answer_callback(bot_token, callback_query_id, text=None):
    api = TELEGRAM_API.format(token=bot_token, method="answerCallbackQuery")
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    async with _telegram_client(25) as client:
        try:
            await client.post(api, data=payload)
        except Exception as e:
            print(f"[telegram] answerCallbackQuery failed: {e}")


async def telegram_send_photo(bot_token, chat_id, photo_url, caption):
    """Скачиваем фото сами и грузим в Telegram файлом, а не просто передаём
    ссылку — CDN Ozon (ir.ozone.ru) отдаёт картинку обычному клиенту (curl,
    браузер), но блокирует запрос, когда его делают серверы самого Telegram
    (sendPhoto по URL падает с 400 "failed to get HTTP URL content"/"wrong
    type of the web page content"). Скачивание с нашей стороны этот антибот
    не задевает."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        img_resp = await client.get(photo_url)
        img_resp.raise_for_status()
        photo_bytes = img_resp.content

    api = TELEGRAM_API.format(token=bot_token, method="sendPhoto")
    data = {"chat_id": chat_id, "caption": caption[:1024]}
    files = {"photo": ("photo.jpg", photo_bytes, "image/jpeg")}
    async with _telegram_client(30) as client:
        r = await client.post(api, data=data, files=files)
        if r.status_code != 200:
            raise RuntimeError(f"sendPhoto failed: {r.status_code} {r.text[:300]}")


async def telegram_send_document(bot_token, chat_id, pdf_bytes, filename, caption=None):
    api = TELEGRAM_API.format(token=bot_token, method="sendDocument")
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption[:1024]
    files = {"document": (filename, pdf_bytes, "application/pdf")}
    async with _telegram_client(60) as client:
        r = await client.post(api, data=data, files=files)
        if r.status_code != 200:
            raise RuntimeError(f"sendDocument failed: {r.status_code} {r.text[:300]}")


async def telegram_get_updates(bot_token, offset, timeout=20):
    """timeout=20 (по умолчанию) — long-poll для непрерывного bot.py: ждём
    до 20 сек, если апдейтов нет. timeout=0 — для разового прохода
    (run_once.py): не ждать, просто вернуть что уже накопилось."""
    api = TELEGRAM_API.format(token=bot_token, method="getUpdates")
    params = {"timeout": timeout, "allowed_updates": json.dumps(["message", "callback_query"])}
    if offset is not None:
        params["offset"] = offset
    async with _telegram_client(max(25, timeout + 5)) as client:
        r = await client.get(api, params=params)
        r.raise_for_status()
        return r.json()


# ============================================================
# === Форматирование
# ============================================================

def render_help() -> str:
    return (
        "Кнопки снизу:\n"
        "📋 Ожидают — что ещё не напечатано\n"
        "🖨 Печать — выбрать этикетки кнопками и собрать PDF (по умолчанию выбраны все — просто исключайте лишнее)\n"
        "🔁 Повтор печати — выбрать кнопками из уже напечатанных и пересобрать PDF\n"
        "📜 История — последние напечатанные\n\n"
        "Команды без кнопок:\n"
        "/merge_select RU-123 RU-456 — собрать PDF по номерам текстом, без кнопок\n"
        "/reprint RU-123 ... — то же для повторной печати\n"
        "/stats — статистика\n"
        "/ping — проверка, что бот жив\n"
    )


def _short_name(products: list) -> str:
    if not products:
        return "?"
    name = products[0].get("name") or products[0].get("offer_id") or "?"
    if len(products) > 1:
        name += f" +{len(products) - 1}"
    return (name[:40] + "…") if len(name) > 40 else name


def _lookback_window():
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    now = datetime.datetime.now(datetime.timezone.utc)
    since = now - datetime.timedelta(days=config.POSTING_LOOKBACK_DAYS)
    return since.strftime(fmt), now.strftime(fmt)


def _extract_products(posting: dict) -> list:
    result = []
    for p in posting.get("products") or []:
        result.append({
            "offer_id": p.get("offer_id"),
            "name": p.get("name"),
            "sku": p.get("sku"),
            "quantity": p.get("quantity", 1),
        })
    return result


# ============================================================
# === Проход A/B — поллинг Ozon
# ============================================================

async def _send_order_message(bot_token, chat_id, photo_url, caption) -> bool:
    if photo_url:
        try:
            await telegram_send_photo(bot_token, chat_id, photo_url, caption)
            return True
        except Exception as e:
            print(f"[notify] фото не отправилось ({chat_id}): {e}")
    return await telegram_send(bot_token, chat_id, caption)


async def send_new_order_notification(bot_token, chat_ids, shop, posting_number, products) -> bool:
    """chat_ids[0] — основной получатель, от его доставки зависит, будет ли
    отправление помечено notified_at (см. poll_once). Остальные — доп.
    получатели best-effort: их сбой логируется, но не блокирует пометку и
    не ретраится бесконечно (иначе один заблокировавший бота человек
    остановил бы уведомления вообще для всех)."""
    offer_ids = [p["offer_id"] for p in products if p.get("offer_id")]
    images = await ozon_client.get_product_main_images(shop, offer_ids) if offer_ids else {}
    photo_url = next((images.get(p.get("offer_id")) for p in products if images.get(p.get("offer_id"))), None)
    names = "\n".join(f"• {p.get('name') or p.get('offer_id')} × {p.get('quantity', 1)}" for p in products)
    caption = f"📦 [{shop.name}] Новое отправление {posting_number}\n{names}"

    ok_primary = await _send_order_message(bot_token, chat_ids[0], photo_url, caption)
    for chat_id in chat_ids[1:]:
        if not await _send_order_message(bot_token, chat_id, photo_url, caption):
            print(f"[notify] доп. получателю {chat_id} не удалось отправить {posting_number}")
    return ok_primary


async def process_label_ready(db, lock):
    """Только забирает этикетку у Ozon и кэширует (label_pdf_cached в БД) —
    раньше сразу же слала её отдельным сообщением на каждое отправление, но
    это убрали по просьбе (шумно, дублирует кнопку "🖨 Печать"/сборный PDF,
    см. run_merge). Название mark_label_sent в db.py осталось как есть,
    хотя теперь по факту означает "получена и закэширована", а не
    "отправлена в Telegram" — переименовывать колонку в БД ради этого не
    стали."""
    rows = await db_module.get_label_pending(db, lock)
    if not rows:
        return

    # Каждый магазин — свои ключи API, поэтому забираем этикетки отдельным
    # батчем на магазин (posting_number из разных магазинов не смешиваем).
    postings_by_shop = {}
    for pn, shop_key, _status, _products_json in rows:
        postings_by_shop.setdefault(shop_key, []).append(pn)

    now = int(time.time())
    for shop_key, posting_numbers in postings_by_shop.items():
        shop = SHOPS_BY_KEY.get(shop_key)
        if shop is None:
            print(f"[label] неизвестный shop_key={shop_key} (магазин удалили из .env?) — пропускаю {posting_numbers}", flush=True)
            continue
        print(f"[label] [{shop.name}] запрашиваю {len(posting_numbers)} этикеток...", flush=True)
        t0 = time.time()
        label_map, error_map = await pdf_label.fetch_labels(shop, posting_numbers)
        print(f"[label] [{shop.name}] получено {len(label_map)}, ошибок {len(error_map)}, "
              f"{time.time() - t0:.1f} сек", flush=True)
        for pn, raw_pdf in label_map.items():
            await db_module.mark_label_sent(db, lock, pn, raw_pdf, now)

        for pn, reason in error_map.items():
            print(f"[label] не удалось получить этикетку {pn}: {reason} (повторим на следующем цикле)")


async def _poll_shop(db, lock, shop) -> set:
    """Проход A для одного магазина: находит новые/готовые отправления,
    возвращает набор posting_number, увиденных в этом цикле (для сверки
    отмен). Полностью изолирован от ошибок другого магазина — если у одного
    магазина Ozon недоступен, второй всё равно опрашивается."""
    since_iso, to_iso = _lookback_window()
    now = int(time.time())
    fetched = set()

    print(f"[poll] [{shop.name}] старт", flush=True)
    for status in ("awaiting_packaging", "awaiting_deliver"):
        t0 = time.time()
        try:
            postings = await ozon_client.iter_fbs_postings(shop, status, since_iso, to_iso)
        except Exception as e:
            print(f"[poll] [{shop.name}] список {status} не получен: {e}", flush=True)
            continue
        print(f"[poll] [{shop.name}] {status}: {len(postings)} шт., {time.time() - t0:.1f} сек", flush=True)
        for p in postings:
            pn = p.get("posting_number")
            if not pn:
                continue
            fetched.add(pn)
            products = _extract_products(p)
            await db_module.upsert_posting(
                db, lock, shop.key, pn, p.get("status", status), products,
                p.get("in_process_at") or p.get("created_at"), now,
            )

    # Отправления этого магазина, пропавшие из выборки по awaiting_*, могли
    # смениться на отменённые/другой статус — уточняем поштучно.
    tracked = await db_module.get_active_tracked_numbers(db, lock, shop.key)
    to_check = [pn for pn in tracked if pn not in fetched]
    print(f"[poll] [{shop.name}] реконсиляция: {len(to_check)} отправлений "
          f"(всего отслеживается {len(tracked)})", flush=True)
    t0 = time.time()
    for i, pn in enumerate(to_check, 1):
        try:
            detail = await ozon_client.get_posting(shop, pn)
        except Exception as e:
            print(f"[poll] [{shop.name}] уточнение статуса {pn} не удалось: {e}", flush=True)
            continue
        status = detail.get("status", "unknown")
        products = _extract_products(detail)
        await db_module.upsert_posting(
            db, lock, shop.key, pn, status, products,
            detail.get("in_process_at") or detail.get("created_at"), now,
        )
        await db_module.mark_cancelled_if_needed(db, lock, pn, status, now)
        if i % 10 == 0 or i == len(to_check):
            print(f"[poll] [{shop.name}] реконсиляция: {i}/{len(to_check)}, "
                  f"{time.time() - t0:.1f} сек", flush=True)

    return fetched


async def poll_once(db, lock, bot_token, chat_ids):
    for shop in config.SHOPS:
        try:
            await _poll_shop(db, lock, shop)
        except Exception as e:
            # Не даём проблеме одного магазина (например, протухшие ключи)
            # остановить опрос остальных магазинов в этом же цикле.
            print(f"[poll] [{shop.name}] цикл упал: {type(e).__name__}: {e}")

    # Уведомления/отмены/этикетки — по всем магазинам сразу (каждая строка
    # несёт свой shop_key, exactly-once гарантируется в БД через
    # notified_at/label_sent_at IS NULL, независимо от того, из какого
    # магазина отправление). chat_ids[0] — основной получатель: doставка
    # ИМЕННО ему решает, помечать ли что-то отправленным (см. комментарии в
    # send_new_order_notification/telegram_send) — иначе сбой доставки
    # молча терялся бы (ошибка логировалась, но notified_at всё равно
    # проставлялся).
    for pn, shop_key, _status, products_json in await db_module.get_unnotified(db, lock):
        products = json.loads(products_json)
        shop = SHOPS_BY_KEY.get(shop_key)
        if shop is None:
            print(f"[notify] неизвестный shop_key={shop_key} для {pn} — пропускаю")
            continue
        try:
            delivered = await send_new_order_notification(bot_token, chat_ids, shop, pn, products)
        except Exception as e:
            print(f"[notify] {pn} не отправилось: {e}")
            continue
        if not delivered:
            print(f"[notify] {pn}: не доставлено основному получателю, повторим на следующем цикле")
            continue
        await db_module.mark_notified(db, lock, pn, int(time.time()))

    for pn, shop_key, products_json in await db_module.get_cancel_unnotified(db, lock):
        products = json.loads(products_json)
        text = (f"❌ [{_shop_name(shop_key)}] Отправление {pn} отменено — этикетки не будет.\n"
                f"{_short_name(products)}")
        delivered = await telegram_send(bot_token, chat_ids[0], text)
        for chat_id in chat_ids[1:]:
            if not await telegram_send(bot_token, chat_id, text):
                print(f"[cancel-notify] доп. получателю {chat_id} не удалось отправить {pn}")
        if not delivered:
            print(f"[cancel-notify] {pn}: не доставлено основному получателю, повторим на следующем цикле")
            continue
        await db_module.mark_cancel_notified(db, lock, pn, int(time.time()))

    await process_label_ready(db, lock)


# ============================================================
# === Выбор этикеток кнопками (общий экран для печати и повтора печати)
# ============================================================

def _postings_from_pending_rows(rows) -> list:
    result = []
    for pn, shop_key, _status, products_json, _label_sent_at in rows:
        products = json.loads(products_json)
        result.append({"pn": pn, "label": f"[{_shop_name(shop_key)}] {_short_name(products)}"})
    return result


def _postings_from_history_rows(rows) -> list:
    result = []
    for pn, shop_key, products_json, printed_at in rows:
        products = json.loads(products_json)
        ts = time.strftime("%d.%m %H:%M", time.localtime(printed_at))
        result.append({"pn": pn, "label": f"[{_shop_name(shop_key)}] {ts} {_short_name(products)}"})
    return result


def _build_selection_session(postings: list, mode: str, default_all_excluded: bool) -> dict:
    """mode: "merge" (печать новых, по умолчанию выбраны все — исключай лишнее)
    или "reprint" (повтор печати, по умолчанию ничего не выбрано — отмечай нужное)."""
    excluded = {p["pn"] for p in postings} if default_all_excluded else set()
    return {
        "mode": mode, "postings": postings, "excluded": excluded, "page": 0,
        "filter": None, "awaiting_search": False,
    }


def _visible_postings(session: dict) -> list:
    """Список отправлений с учётом активного поискового фильтра (если есть) —
    поиск подстрокой по номеру отправления или по названию/магазину, без
    учёта регистра."""
    postings = session["postings"]
    query = (session.get("filter") or "").strip().lower()
    if not query:
        return postings
    return [p for p in postings if query in p["pn"].lower() or query in p["label"].lower()]


def _selection_header(session: dict) -> str:
    total = len(session["postings"])
    included = total - len(session["excluded"])
    prefix = ""
    query = session.get("filter")
    if query:
        prefix = f"🔎 «{query}»: найдено {len(_visible_postings(session))}. "
    if session["mode"] == "reprint":
        return f"{prefix}Повтор печати — выбрано {included} из {total}. Нажимайте на отправления, чтобы отметить:"
    return f"{prefix}Печать этикеток — выбрано {included} из {total}. По умолчанию выбраны все, нажимайте, чтобы исключить:"


def _render_selection_keyboard(session: dict) -> dict:
    visible = _visible_postings(session)
    total_pages = max(1, (len(visible) + MERGE_PAGE_SIZE - 1) // MERGE_PAGE_SIZE)
    page = max(0, min(session["page"], total_pages - 1))
    session["page"] = page
    chunk = visible[page * MERGE_PAGE_SIZE: (page + 1) * MERGE_PAGE_SIZE]

    rows_kb = []
    if not visible:
        rows_kb.append([{"text": "Ничего не найдено", "callback_data": "noop"}])
    for item in chunk:
        pn = item["pn"]
        mark = "⬜" if pn in session["excluded"] else "✅"
        rows_kb.append([{"text": f"{mark} {pn} — {item['label']}", "callback_data": f"tog:{pn}"}])
    if total_pages > 1:
        rows_kb.append([
            {"text": "«", "callback_data": f"pg:{page - 1}"},
            {"text": f"{page + 1}/{total_pages}", "callback_data": "noop"},
            {"text": "»", "callback_data": f"pg:{page + 1}"},
        ])
    # "Отметить/снять все" действуют на то, что видно сейчас (весь список,
    # либо только результаты поиска, если активен фильтр).
    rows_kb.append([
        {"text": "☑️ Отметить все", "callback_data": "selall"},
        {"text": "⬜ Снять все", "callback_data": "selnone"},
    ])
    search_row = [{"text": "🔎 Найти по номеру", "callback_data": "search"}]
    if session.get("filter"):
        search_row.append({"text": "❌ Сбросить поиск", "callback_data": "clearfilter"})
    rows_kb.append(search_row)
    included = len(session["postings"]) - len(session["excluded"])
    gen_label = "🔁 Напечатать повторно" if session["mode"] == "reprint" else "🖨 Сформировать PDF"
    rows_kb.append([
        {"text": f"{gen_label} ({included})", "callback_data": "gen"},
        {"text": "✖ Отмена", "callback_data": "cancel"},
    ])
    return {"inline_keyboard": rows_kb}


async def run_merge(db, lock, bot_token, chat_id, posting_numbers: list, is_reprint: bool = False):
    posting_numbers = list(dict.fromkeys(posting_numbers))  # dedupe, порядок сохраняем
    rows = await db_module.get_postings_by_numbers(db, lock, posting_numbers)
    found = {r[0]: r for r in rows}  # pn -> (pn, shop_key, status, products_json, label_pdf_cached, printed_at, cancelled_at)

    missing = [pn for pn in posting_numbers if pn not in found]
    cancelled = [pn for pn in posting_numbers if pn in found and found[pn][6]]
    no_label = [pn for pn in posting_numbers if pn in found and not found[pn][6] and not found[pn][4]]
    usable = [pn for pn in posting_numbers if pn in found and found[pn][4] and not found[pn][6]]

    if not usable:
        await telegram_send(bot_token, chat_id, "Нечего печатать — ни для одного номера нет готовой этикетки.")
        return

    pdf_parts = []
    for pn in usable:
        _, _shop_key, _status, products_json, raw_pdf, _printed_at, _cancelled_at = found[pn]
        products = json.loads(products_json)
        lines = pdf_label.format_offer_id_lines(products)
        try:
            pdf_parts.append(pdf_label.overlay_offer_ids(raw_pdf, lines))
        except Exception as e:
            print(f"[merge] наложение для {pn} не удалось: {e}")
            pdf_parts.append(raw_pdf)

    ts = time.strftime("%Y-%m-%d_%H-%M", time.localtime())
    summary = [f"Собрано этикеток: {len(usable)}"]
    if missing:
        summary.append("не найдено: " + ", ".join(missing))
    if no_label:
        summary.append("нет готовой этикетки: " + ", ".join(no_label))
    if cancelled:
        summary.append("отменено: " + ", ".join(cancelled))
    caption = "\n".join(summary)

    merged = pdf_label.merge_pdfs(pdf_parts)
    if len(merged) > config.TELEGRAM_MAX_DOCUMENT_BYTES:
        # Грубая, но надёжная защита от лимита Telegram (50 МБ) — делим пополам.
        mid = len(pdf_parts) // 2 or 1
        for idx, part in enumerate((pdf_parts[:mid], pdf_parts[mid:]), start=1):
            if not part:
                continue
            await telegram_send_document(bot_token, chat_id, pdf_label.merge_pdfs(part), f"labels_{ts}_part{idx}.pdf")
        await telegram_send(bot_token, chat_id, caption + "\n(файл слишком большой — разбит на части)")
    else:
        await telegram_send_document(bot_token, chat_id, merged, f"labels_{ts}.pdf", caption)

    if not is_reprint:
        await db_module.mark_printed(db, lock, usable, int(time.time()))


# ============================================================
# === Команды и колбэки
# ============================================================

async def _apply_search(db, lock, bot_token, chat_id, session, query):
    session["filter"] = query
    session["page"] = 0
    if not _visible_postings(session):
        session["filter"] = None
        await telegram_send(bot_token, chat_id, f"Не найдено отправлений по «{query}».")
    new_message_id = await telegram_send_keyboard(bot_token, chat_id, _selection_header(session),
                                                    _render_selection_keyboard(session))
    session["message_id"] = new_message_id


async def handle_command(db, lock, bot_token, chat_id, text, merge_sessions):
    stripped = text.strip()
    session = merge_sessions.get(chat_id)
    if session and session.get("awaiting_search"):
        session["awaiting_search"] = False
        if stripped not in BUTTON_TEXT_TO_COMMAND and not stripped.startswith("/"):
            await _apply_search(db, lock, bot_token, chat_id, session, stripped)
            return
        # пользователь нажал другую кнопку/команду вместо ответа на поиск —
        # просто отменяем ожидание и обрабатываем как обычно ниже.

    text = BUTTON_TEXT_TO_COMMAND.get(stripped, text)  # нажатие кнопки снизу -> та же команда
    parts = text.strip().split()
    if not parts:
        return
    cmd, args = parts[0].lower(), parts[1:]

    if cmd == "/start":
        await telegram_send(bot_token, chat_id, f"Бот запущен. Ваш chat_id: {chat_id}\n\n{render_help()}",
                             reply_markup=MAIN_KEYBOARD)
    elif cmd == "/help":
        await telegram_send(bot_token, chat_id, render_help())
    elif cmd == "/ping":
        await telegram_send(bot_token, chat_id, "pong 🏓")
    elif cmd == "/stats":
        stats = await db_module.get_stats(db, lock)
        by_shop = await db_module.get_stats_by_shop(db, lock)
        lines = [
            f"Всего отслежено: {stats['total']}",
            f"Ожидают печати: {stats['pending']}",
            f"Напечатано: {stats['printed']}",
            f"Отменено: {stats['cancelled']}",
        ]
        if len(config.SHOPS) > 1:
            lines.append("")
            for shop in config.SHOPS:
                s = by_shop.get(shop.key, {"total": 0, "pending": 0, "printed": 0})
                lines.append(f"— {shop.name}: всего {s['total']}, ожидают {s['pending']}, напечатано {s['printed']}")
        await telegram_send(bot_token, chat_id, "\n".join(lines))
    elif cmd == "/pending":
        rows = await db_module.get_pending_for_merge(db, lock, label_required=False)
        if not rows:
            await telegram_send(bot_token, chat_id, "Нет неотправленных заказов.")
        else:
            lines = []
            for pn, shop_key, _status, products_json, label_sent_at in rows:
                products = json.loads(products_json)
                tag = "🏷" if label_sent_at else "⏳ждём этикетку"
                lines.append(f"{tag} [{_shop_name(shop_key)}] {pn} — {_short_name(products)}")
            await telegram_send(bot_token, chat_id, "\n".join(lines))
    elif cmd == "/merge":
        rows = await db_module.get_pending_for_merge(db, lock, label_required=True)
        if not rows:
            await telegram_send(bot_token, chat_id, "Нет готовых этикеток для сборки.")
            return
        session = _build_selection_session(_postings_from_pending_rows(rows), mode="merge", default_all_excluded=False)
        keyboard = _render_selection_keyboard(session)
        message_id = await telegram_send_keyboard(bot_token, chat_id, _selection_header(session), keyboard)
        session["message_id"] = message_id
        merge_sessions[chat_id] = session
    elif cmd == "/merge_select":
        if not args:
            await telegram_send(bot_token, chat_id,
                                 "Укажите номера отправлений через пробел: /merge_select RU-123 RU-456\n"
                                 "Либо нажмите «🖨 Печать» ниже, чтобы выбрать кнопками.")
            return
        await run_merge(db, lock, bot_token, chat_id, args)
    elif cmd == "/reprint":
        if args:
            await run_merge(db, lock, bot_token, chat_id, args, is_reprint=True)
            return
        rows = await db_module.get_history(db, lock, limit=50)
        if not rows:
            await telegram_send(bot_token, chat_id, "История пуста — пока нечего печатать повторно.")
            return
        session = _build_selection_session(_postings_from_history_rows(rows), mode="reprint", default_all_excluded=True)
        keyboard = _render_selection_keyboard(session)
        message_id = await telegram_send_keyboard(bot_token, chat_id, _selection_header(session), keyboard)
        session["message_id"] = message_id
        merge_sessions[chat_id] = session
    elif cmd == "/history":
        limit = int(args[0]) if args and args[0].isdigit() else 30
        rows = await db_module.get_history(db, lock, limit)
        if not rows:
            await telegram_send(bot_token, chat_id, "История пуста.")
        else:
            lines = []
            for pn, shop_key, products_json, printed_at in rows:
                products = json.loads(products_json)
                ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(printed_at))
                lines.append(f"{ts} — [{_shop_name(shop_key)}] {pn} — {_short_name(products)}")
            await telegram_send(bot_token, chat_id, "\n".join(lines))
    else:
        await telegram_send(bot_token, chat_id, "Неизвестная команда. /help — список команд.")


async def handle_callback(db, lock, bot_token, chat_id, callback_query, merge_sessions):
    data = callback_query.get("data", "")
    callback_id = callback_query["id"]

    if data == "noop":
        await telegram_answer_callback(bot_token, callback_id)
        return

    session = merge_sessions.get(chat_id)
    if session is None:
        await telegram_answer_callback(bot_token, callback_id, "Сессия устарела, откройте заново кнопкой")
        return
    message_id = session["message_id"]

    if data.startswith("tog:"):
        pn = data[4:]
        if pn in session["excluded"]:
            session["excluded"].discard(pn)
        else:
            session["excluded"].add(pn)
        await telegram_edit_message(bot_token, chat_id, message_id, _selection_header(session), _render_selection_keyboard(session))
        await telegram_answer_callback(bot_token, callback_id)
    elif data.startswith("pg:"):
        session["page"] = int(data[3:])
        await telegram_edit_message(bot_token, chat_id, message_id, _selection_header(session), _render_selection_keyboard(session))
        await telegram_answer_callback(bot_token, callback_id)
    elif data == "selall":
        # Действует на видимые сейчас (весь список либо только результаты поиска).
        for p in _visible_postings(session):
            session["excluded"].discard(p["pn"])
        await telegram_edit_message(bot_token, chat_id, message_id, _selection_header(session), _render_selection_keyboard(session))
        await telegram_answer_callback(bot_token, callback_id, "Отметил")
    elif data == "selnone":
        for p in _visible_postings(session):
            session["excluded"].add(p["pn"])
        await telegram_edit_message(bot_token, chat_id, message_id, _selection_header(session), _render_selection_keyboard(session))
        await telegram_answer_callback(bot_token, callback_id, "Снял")
    elif data == "search":
        session["awaiting_search"] = True
        await telegram_answer_callback(bot_token, callback_id)
        await telegram_send(bot_token, chat_id,
                             "Введите номер отправления (или часть номера/название товара) — покажу только совпадения:")
    elif data == "clearfilter":
        session["filter"] = None
        session["page"] = 0
        await telegram_edit_message(bot_token, chat_id, message_id, _selection_header(session), _render_selection_keyboard(session))
        await telegram_answer_callback(bot_token, callback_id)
    elif data == "cancel":
        merge_sessions.pop(chat_id, None)
        await telegram_edit_message(bot_token, chat_id, message_id, "Отменено.")
        await telegram_answer_callback(bot_token, callback_id)
    elif data == "gen":
        included = [p["pn"] for p in session["postings"] if p["pn"] not in session["excluded"]]
        is_reprint = session["mode"] == "reprint"
        merge_sessions.pop(chat_id, None)
        await telegram_answer_callback(bot_token, callback_id)
        if included:
            await telegram_edit_message(bot_token, chat_id, message_id, f"Собираю PDF ({len(included)})…")
            await run_merge(db, lock, bot_token, chat_id, included, is_reprint=is_reprint)
        else:
            await telegram_edit_message(bot_token, chat_id, message_id, "Ничего не выбрано.")
    else:
        await telegram_answer_callback(bot_token, callback_id)


# ============================================================
# === main / run
# ============================================================

async def main():
    db = await db_module.init_db()
    lock = asyncio.Lock()
    merge_sessions = {}

    saved_offset = await db_module.kv_get(db, lock, "tg_offset")
    offset = int(saved_offset) + 1 if saved_offset else None

    print(f"[bot] Запущен. Поллинг Ozon каждые {config.POLL_SECONDS} сек.")
    try:
        # Клавиатура — всем админам (config.ADMIN_CHAT_IDS): каждый из них
        # может управлять ботом, кнопки работают у всех одинаково.
        for chat_id in config.NOTIFY_CHAT_IDS:
            await telegram_send(config.BOT_TOKEN, chat_id, "🤖 Бот запущен.", reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        print(f"[bot] стартовое сообщение не отправилось: {e}")

    async def polling_worker():
        while True:
            try:
                await poll_once(db, lock, config.BOT_TOKEN, config.NOTIFY_CHAT_IDS)
            except Exception as e:
                print(f"[poll] цикл упал: {type(e).__name__}: {e}")
            await asyncio.sleep(config.POLL_SECONDS)

    async def telegram_worker():
        nonlocal offset
        while True:
            try:
                resp = await telegram_get_updates(config.BOT_TOKEN, offset)
            except Exception as e:
                print(f"[telegram] getUpdates не удался: {e}")
                await asyncio.sleep(5)
                continue
            for update in resp.get("result", []):
                offset = update["update_id"] + 1
                await db_module.kv_set(db, lock, "tg_offset", offset)
                try:
                    message = update.get("message")
                    callback_query = update.get("callback_query")
                    if message and "text" in message:
                        msg_chat_id = str(message["chat"]["id"])
                        if msg_chat_id not in config.ADMIN_CHAT_IDS:
                            # Печатаем chat_id постороннего отправителя, чтобы
                            # его можно было найти в логах при добавлении
                            # нового человека в EXTRA_CHAT_IDS (см. README).
                            print(f"[telegram] сообщение от постороннего chat_id={msg_chat_id}, игнорирую: {message['text'][:80]!r}")
                            continue
                        await handle_command(db, lock, config.BOT_TOKEN, msg_chat_id, message["text"], merge_sessions)
                    elif callback_query:
                        cb_chat_id = str(callback_query["message"]["chat"]["id"])
                        if cb_chat_id not in config.ADMIN_CHAT_IDS:
                            continue
                        await handle_callback(db, lock, config.BOT_TOKEN, cb_chat_id, callback_query, merge_sessions)
                except Exception as e:
                    print(f"[telegram] обработка апдейта упала: {type(e).__name__}: {e}")

    async def heartbeat_worker():
        if config.HEARTBEAT_SECONDS <= 0:
            return
        while True:
            await asyncio.sleep(config.HEARTBEAT_SECONDS)
            try:
                stats = await db_module.get_stats(db, lock)
                text = f"💓 Бот жив. Ожидают печати: {stats['pending']}, напечатано всего: {stats['printed']}"
                for chat_id in config.NOTIFY_CHAT_IDS:
                    await telegram_send(config.BOT_TOKEN, chat_id, text)
            except Exception as e:
                print(f"[heartbeat] не удался: {e}")

    async def webapp_worker():
        if not config.WEBAPP_PUBLIC_URL:
            return  # мини-апп не настроен — как и раньше, ничего не меняется
        try:
            await webapp_server.run_embedded(db, lock)
        except Exception as e:
            print(f"[webapp] упал: {type(e).__name__}: {e}")

    await asyncio.gather(
        polling_worker(),
        telegram_worker(),
        heartbeat_worker(),
        webapp_worker(),
    )


def run():
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            print("[bot] Остановлен пользователем.")
            break
        except Exception as e:
            print(f"[bot] Упал с ошибкой: {type(e).__name__}: {e}. Перезапуск через 5 сек.")
            time.sleep(5)


if __name__ == "__main__":
    run()
