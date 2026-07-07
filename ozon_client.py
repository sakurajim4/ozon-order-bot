"""Асинхронная обёртка над Ozon Seller API.

Паттерн ретраев на 429 и разбор ошибок портированы из
~/Documents/ozon-analytics/app.py (ozon_call/_http/parse_ozon_error).

Часть путей/полей помечена "ПРОВЕРИТЬ" — их нужно свериться с реальным
ответом Ozon (см. scripts/verify_label_api_assumptions.py) прежде чем
полностью доверять этой обвязке в бою.
"""
import asyncio
import json

import httpx

import config

OZON_API = "https://api-seller.ozon.ru"

# Отдельные, более щедрые паузы для методов, которые Ozon сильнее ограничивает.
RETRY_429 = {
    "/v1/analytics/data": [12, 25, 50],
}
RETRY_DEFAULT = [2, 5, 12]


class OzonAPIError(Exception):
    def __init__(self, status, error):
        self.status = status
        self.error = error  # {"code": ..., "message": "..."}
        super().__init__(f"Ozon API {status}: {error.get('message')}")


def _headers(shop: "config.Shop"):
    return {
        "Client-Id": str(shop.client_id),
        "Api-Key": str(shop.api_key),
        "Content-Type": "application/json",
    }


async def _http(shop, path, body, method, delays):
    url = OZON_API + path
    headers = _headers(shop)
    attempt = 0
    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            try:
                if method == "GET":
                    resp = await client.get(url, headers=headers, params=body or {})
                else:
                    resp = await client.request(method, url, headers=headers, json=body or {})
            except httpx.HTTPError as e:
                return 0, str(e).encode(), "text/plain"
            if resp.status_code == 429 and attempt < len(delays):
                await asyncio.sleep(delays[attempt])
                attempt += 1
                continue
            return resp.status_code, resp.content, resp.headers.get("content-type", "")


def parse_ozon_error(status, raw: bytes) -> dict:
    try:
        j = json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        j = {}
    msg = j.get("message") or (j.get("error") or {}).get("message") or ""
    code = j.get("code") or (j.get("error") or {}).get("code") or status
    human = {
        401: "Ozon не принял ключи (401). Проверьте SHOP_N_CLIENT_ID/SHOP_N_API_KEY в .env.",
        403: "Доступ запрещён (403). Проверьте права API-ключа в кабинете Ozon.",
        404: "Метод не найден (404) — возможно, Ozon изменил версию API.",
        429: "Превышен лимит запросов Ozon (429).",
    }.get(status, "")
    text = msg or human or f"Ошибка Ozon API, HTTP {status}"
    if msg and human:
        text = f"{human} — {msg}"
    return {"code": code, "message": text}


async def ozon_call(shop, path, body, method="POST") -> tuple:
    """JSON-вызов от имени указанного магазина. Возвращает (status, dict)."""
    status, raw, _ctype = await _http(shop, path, body, method, RETRY_429.get(path, RETRY_DEFAULT))
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        data = {"_raw": raw.decode("utf-8", "replace")}
    return status, data


async def ozon_call_binary(shop, path, body, method="POST") -> tuple:
    """Вызов, где успех - сырые байты (например, PDF этикетки).
    Возвращает (status, raw_bytes, is_binary)."""
    status, raw, ctype = await _http(shop, path, body, method, RETRY_429.get(path, RETRY_DEFAULT))
    is_binary = status == 200 and "application/json" not in (ctype or "")
    return status, raw, is_binary


# ------------------------------------------------------------- postings ----

async def iter_fbs_postings(shop, status: str, since_iso: str, to_iso: str, page_limit: int = 100) -> list:
    """Все FBS/rFBS-отправления магазина shop в заданном статусе за период [since, to].

    Подтверждено вживую (2026-07): filter.status/since/to и result.postings —
    именно так, как реализовано ниже; посылка несёт posting_number, status,
    in_process_at, is_multibox/multi_box_qty и products[] (offer_id, name,
    sku, quantity) — всё это уже используется в bot.py/db.py как есть.
    """
    offset = 0
    postings = []
    while True:
        body = {
            "dir": "ASC",
            "filter": {"status": status, "since": since_iso, "to": to_iso},
            "limit": page_limit,
            "offset": offset,
            "with": {"analytics_data": False, "financial_data": False},
        }
        api_status, data = await ozon_call(shop, "/v3/posting/fbs/list", body)
        if api_status != 200:
            raise OzonAPIError(api_status, data if "message" in data else parse_ozon_error(api_status, json.dumps(data).encode()))
        result = data.get("result") or {}
        page = result.get("postings") or []
        postings.extend(page)
        if not result.get("has_next") or not page:
            break
        offset += page_limit
    return postings


async def get_posting(shop, posting_number: str) -> dict:
    """Детали одного отправления через /v3/posting/fbs/get.

    Подтверждено вживую (2026-07): /v2/posting/fbs/get больше не существует
    (голый 404 "page not found", не JSON-ошибка) — Ozon перевёл метод на v3,
    структура ответа (result{...}) не изменилась.
    """
    body = {
        "posting_number": posting_number,
        "with": {"analytics_data": False, "financial_data": False},
    }
    api_status, data = await ozon_call(shop, "/v3/posting/fbs/get", body)
    if api_status != 200:
        raise OzonAPIError(api_status, parse_ozon_error(api_status, json.dumps(data).encode()))
    return (data.get("result") or {})


# -------------------------------------------------------- product photos ----

async def get_product_main_images(shop, offer_ids: list) -> dict:
    """offer_id -> URL главного фото (или None, если не нашли).

    Подтверждено вживую (2026-07): /v3/product/info/list отвечает плоским
    {"items": [...]} без обёртки "result", и картинки лежат в item["images"]
    (просто список URL на ir.ozone.ru, первый — главный); поля primary_image
    в реальном ответе не встретилось — оставлен как запасной вариант на
    случай, если Ozon добавит его или вернёт для других категорий товаров.

    ВАЖНО: URL с ir.ozone.ru отдаёт картинку обычному клиенту, но блокирует
    запрос с серверов Telegram (антибот CDN) — поэтому bot.py.telegram_send_photo
    сам скачивает файл и грузит как multipart, а не передаёт эту ссылку Telegram напрямую.
    """
    if not offer_ids:
        return {}
    result = {}
    chunk_size = 100
    for i in range(0, len(offer_ids), chunk_size):
        chunk = offer_ids[i:i + chunk_size]
        body = {"offer_id": chunk}
        api_status, data = await ozon_call(shop, "/v3/product/info/list", body)
        if api_status != 200:
            for offer_id in chunk:
                result[offer_id] = None
            continue
        items = (data.get("result") or {}).get("items") or data.get("items") or []
        for item in items:
            offer_id = item.get("offer_id")
            image = None
            primary = item.get("primary_image")
            if isinstance(primary, list) and primary:
                image = primary[0]
            elif isinstance(primary, str) and primary:
                image = primary
            if not image:
                images = item.get("images") or []
                if images:
                    image = images[0]
            if offer_id:
                result[offer_id] = image
        for offer_id in chunk:
            result.setdefault(offer_id, None)
    return result


# -------------------------------------------------------------- labels ----

async def get_label_pdf(shop, posting_numbers: list) -> tuple:
    """Этикетки для батча (<=20) posting_number одного магазина.

    Возвращает (True, pdf_bytes) при успехе или (False, error_dict) —
    у Ozon этот вызов all-or-nothing: одно неверное отправление роняет
    весь батч (см. план, раздел "пайплайн этикеток" в pdf_label.py).
    """
    if not posting_numbers:
        return False, {"code": "empty", "message": "Пустой список отправлений"}
    body = {"posting_number": posting_numbers}
    status, raw, is_binary = await ozon_call_binary(shop, "/v2/posting/fbs/package-label", body)
    if status == 200 and is_binary:
        return True, raw
    return False, parse_ozon_error(status, raw)
