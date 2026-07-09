"""Backend Telegram Mini App'а — СТАНДАЛОН, отдельный процесс от bot.py.

Пока фича не выкатывается на прод (см. план) — гоняется только локально,
рядом с боевым ботом на VPS не участвует ни в поллинге Ozon, ни в
getUpdates, поэтому не может вызвать дубли уведомлений. Открывает ТУ ЖЕ
bot_state.sqlite3, но строго на чтение (PRAGMA query_only) — эта фича
никогда не пишет в БД и не помечает отправления напечатанными
(printed_at/label_sent_at — состояние /merge в bot.py, отдельное от
picking-list, см. picking_list.py).

Запуск: python3 webapp_server.py (слушает 127.0.0.1:config.WEBAPP_PORT).
"""
import asyncio
import hashlib
import hmac
import json
import time
import urllib.parse
from pathlib import Path

import httpx
from aiohttp import web

import bot
import config
import db as db_module
import ozon_client
import picking_list

WEBAPP_DIR = Path(__file__).parent / "webapp"

# initData считаем свежим не дольше суток — не пускаем протухшие/утёкшие ссылки.
INIT_DATA_MAX_AGE = 24 * 3600


def _validate_init_data(init_data: str):
    """Проверка подписи Telegram Web App initData (стандартный алгоритм:
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).
    Возвращает dict пользователя (поле "user" из initData) или None, если
    подпись неверна/данные протухли/формат не тот."""
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed_hash, received_hash):
        return None
    auth_date = parsed.get("auth_date")
    try:
        if not auth_date or time.time() - int(auth_date) > INIT_DATA_MAX_AGE:
            return None
    except ValueError:
        return None
    try:
        user = json.loads(parsed.get("user", ""))
    except Exception:
        return None
    return user if isinstance(user, dict) and user.get("id") else None


async def _require_admin(request) -> dict:
    """Тот же периметр доступа, что и у команд бота (config.ADMIN_CHAT_IDS) —
    отдельной системы прав для мини-аппа нет."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("tma "):
        raise web.HTTPUnauthorized(text="Нет initData (заголовок Authorization: tma <initData>)")
    user = _validate_init_data(auth[len("tma "):])
    if user is None:
        raise web.HTTPUnauthorized(text="Некорректная или протухшая подпись initData")
    if str(user["id"]) not in config.ADMIN_CHAT_IDS:
        raise web.HTTPForbidden(text="Этот chat_id не в списке админов бота")
    return user


# ============================================================
# === API
# ============================================================

async def handle_pending(request):
    await _require_admin(request)
    db, lock = request.app["db"], request.app["lock"]

    rows = await db_module.get_pending_for_webapp(db, lock)
    by_shop = {}
    for pn, shop_key, products_json, first_seen_at in rows:
        by_shop.setdefault(shop_key, []).append((pn, json.loads(products_json), first_seen_at))

    shops_out = []
    for shop_key, entries in by_shop.items():
        shop = bot.SHOPS_BY_KEY.get(shop_key)
        shop_name = shop.name if shop else f"магазин {shop_key}"
        offer_ids = sorted({
            p.get("offer_id") for _pn, products, _ts in entries for p in products if p.get("offer_id")
        })
        images = await ozon_client.get_product_main_images(shop, offer_ids) if (shop and offer_ids) else {}

        postings_out = []
        for pn, products, first_seen_at in entries:
            photo_url = next(
                (images.get(p.get("offer_id")) for p in products if images.get(p.get("offer_id"))), None,
            )
            postings_out.append({
                "pn": pn,
                "name": bot._short_name(products),
                "qty": (products[0].get("quantity", 1) if products else 1),
                "photo_url": photo_url,
                "first_seen_at": first_seen_at,
                "first_seen_label": time.strftime("%d.%m %H:%M", time.localtime(first_seen_at)),
            })
        shops_out.append({"shop_key": shop_key, "shop_name": shop_name, "postings": postings_out})

    shops_out.sort(key=lambda s: s["shop_name"])
    return web.json_response({"shops": shops_out})


async def handle_picking_list(request):
    user = await _require_admin(request)
    db, lock = request.app["db"], request.app["lock"]

    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="Некорректный JSON")
    posting_numbers = body.get("posting_numbers") or []
    if not posting_numbers or not isinstance(posting_numbers, list):
        raise web.HTTPBadRequest(text="Пустой список posting_numbers")

    rows = await db_module.get_postings_by_numbers(db, lock, posting_numbers)
    found = {r[0]: r for r in rows}

    # offer_id -> photo_url отдельным батчем на магазин (свои ключи API).
    offer_ids_by_shop = {}
    for pn in posting_numbers:
        row = found.get(pn)
        if not row or row[6]:  # нет в БД или отменено (cancelled_at)
            continue
        for p in json.loads(row[3]):
            if p.get("offer_id"):
                offer_ids_by_shop.setdefault(row[1], set()).add(p["offer_id"])

    images_by_shop = {}
    for shop_key, offer_ids in offer_ids_by_shop.items():
        shop = bot.SHOPS_BY_KEY.get(shop_key)
        if shop:
            images_by_shop[shop_key] = await ozon_client.get_product_main_images(shop, list(offer_ids))

    items = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for pn in posting_numbers:
            row = found.get(pn)
            if not row or row[6]:
                continue
            shop_key, products_json = row[1], row[3]
            products = json.loads(products_json)
            if not products:
                continue
            images = images_by_shop.get(shop_key, {})
            photo_url = next(
                (images.get(p.get("offer_id")) for p in products if images.get(p.get("offer_id"))), None,
            )
            photo_bytes = None
            if photo_url:
                try:
                    r = await client.get(photo_url)
                    if r.status_code == 200:
                        photo_bytes = r.content
                except Exception as e:
                    print(f"[webapp] фото не скачалось для {pn}: {e}")
            items.append({
                "photo_bytes": photo_bytes,
                "name": bot._short_name(products),
                "qty": products[0].get("quantity", 1),
            })

    if not items:
        raise web.HTTPBadRequest(text="Нечего собирать — все номера не найдены или отменены")

    pdf_bytes = picking_list.build_pdf(items)
    ts = time.strftime("%Y-%m-%d_%H-%M")
    chat_id = str(user["id"])
    try:
        await bot.telegram_send_document(
            config.BOT_TOKEN, chat_id, pdf_bytes, f"picking_list_{ts}.pdf",
            caption=f"🗂 Список на сборку: {len(items)} поз.",
        )
    except Exception as e:
        print(f"[webapp] отправка PDF пользователю {chat_id} не удалась: {e}")
        raise web.HTTPInternalServerError(text="Не удалось отправить PDF в чат — попробуйте ещё раз")

    return web.json_response({"ok": True, "items": len(items)})


async def handle_print_labels(request):
    """Настоящие этикетки Ozon с наложенным артикулом — то же самое, что
    делает /merge в самом боте (bot.run_merge), просто запущено отсюда.
    Переиспользуем run_merge как есть: те же проверки (нет/отменено/нет
    готовой этикетки), та же пометка printed_at, тот же формат ответа в
    чат — никакой отдельной логики печати тут нет."""
    user = await _require_admin(request)
    db, lock = request.app["db"], request.app["lock"]

    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="Некорректный JSON")
    posting_numbers = body.get("posting_numbers") or []
    if not posting_numbers or not isinstance(posting_numbers, list):
        raise web.HTTPBadRequest(text="Пустой список posting_numbers")

    chat_id = str(user["id"])
    try:
        await bot.run_merge(db, lock, config.BOT_TOKEN, chat_id, posting_numbers, is_reprint=False)
    except Exception as e:
        print(f"[webapp] печать этикеток для {chat_id} не удалась: {e}")
        raise web.HTTPInternalServerError(text="Не удалось собрать/отправить этикетки — попробуйте ещё раз")

    return web.json_response({"ok": True})


# ============================================================
# === Статика
# ============================================================

# Без Cache-Control WebView Telegram может закешировать старую версию
# одного файла (например app.js) вместе со свежей версией другого
# (index.html) — при активной разработке это уже один раз привело к тому,
# что новый JS ссылался на элемент, которого не было в закэшированном
# старом HTML, и список молча переставал рендериться. no-cache вместо
# no-store — чтобы ETag/304 всё ещё работали, просто не доверяя локальному
# кэшу вслепую.
_NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


async def handle_index(request):
    return web.FileResponse(WEBAPP_DIR / "index.html", headers=_NO_CACHE)


async def handle_app_js(request):
    return web.FileResponse(WEBAPP_DIR / "app.js", headers=_NO_CACHE)


async def handle_style_css(request):
    return web.FileResponse(WEBAPP_DIR / "style.css", headers=_NO_CACHE)


# ============================================================
# === Запуск
# ============================================================

async def build_app(db, lock) -> web.Application:
    app = web.Application()
    app["db"], app["lock"] = db, lock
    app.router.add_get("/api/pending", handle_pending)
    app.router.add_post("/api/picking-list", handle_picking_list)
    app.router.add_post("/api/print-labels", handle_print_labels)
    app.router.add_get("/", handle_index)
    app.router.add_get("/app.js", handle_app_js)
    app.router.add_get("/style.css", handle_style_css)
    return app


async def run_embedded(db, lock):
    """Встраивается как ещё один воркер в bot.py.main() (asyncio.gather) —
    делит ОДНО соединение с БД с остальным ботом (поллингом и т.д.), поэтому
    в отличие от standalone-режима НЕ ставит PRAGMA query_only (это сломало
    бы запись для всего остального бота). Сама эта фича по-прежнему ничего
    не пишет — просто соединение общее, а не отдельное read-only."""
    app = await build_app(db, lock)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", config.WEBAPP_PORT)
    await site.start()
    print(f"[webapp] слушаю http://127.0.0.1:{config.WEBAPP_PORT} "
          f"(снаружи: {config.WEBAPP_PUBLIC_URL})", flush=True)
    await asyncio.Event().wait()  # держим воркер живым бесконечно


async def _init_app():
    """Standalone-режим (python3 webapp_server.py напрямую, локальная
    разработка/тест) — своё соединение, специально read-only (см. план)."""
    db = await db_module.init_db()
    await db.execute("PRAGMA query_only = ON;")  # страховка: эта фича никогда не пишет в БД
    lock = asyncio.Lock()
    return await build_app(db, lock)


if __name__ == "__main__":
    print(f"[webapp] слушаю http://127.0.0.1:{config.WEBAPP_PORT} "
          f"(локальный тест — см. план, на VPS этот сервер не запущен)")
    web.run_app(_init_app(), host="127.0.0.1", port=config.WEBAPP_PORT)
