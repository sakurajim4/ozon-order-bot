#!/usr/bin/env python3
"""Разовый скрипт для проверки предположений об Ozon Seller API вживую —
ЗАПУСКАТЬ ВРУЧНУЮ один раз, когда появятся реальные ключи и хотя бы один
реальный FBS/rFBS-заказ, ДО того как полагаться на "умную" пакетную версию
пайплайна этикеток (см. план: раздел "Проверить перед кодом").

Что проверяет, по порядку:
  1. Реальные поля ответа /v3/posting/fbs/list (имя фильтра статуса, даты).
  2. Поле главного фото товара в product-info.
  3. Одна этикетка на одно отправление — реальный размер страницы (mediabox).
  4. Батч из нескольких отправлений — порядок страниц совпадает со входным
     списком posting_number?
  5. Намеренно невалидный posting_number в батче — точная форма ошибки Ozon.
  6. (best-effort) отдаёт ли package-label этикетку для отправления вне
     awaiting_deliver.

Ничего не изменяет в БД бота и не отправляет ничего в Telegram — только
печатает диагностику и сохраняет пробные PDF в scripts/output/ для
визуальной проверки (открыть в Preview.app).
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402  (нужен .env рядом с bot.py)
import ozon_client  # noqa: E402
import pdf_label  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


def _save(name: str, data: bytes):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, name)
    with open(path, "wb") as f:
        f.write(data)
    print(f"    -> сохранено: {path}")


async def step1_list_postings(shop):
    print("\n=== 1. /v3/posting/fbs/list — реальные поля ===")
    since_iso, to_iso = ozon_client_lookback()
    all_postings = []
    for status in ("awaiting_packaging", "awaiting_deliver"):
        try:
            postings = await ozon_client.iter_fbs_postings(shop, status, since_iso, to_iso)
        except Exception as e:
            print(f"  [{status}] ошибка: {e}")
            continue
        print(f"  [{status}] найдено: {len(postings)}")
        all_postings.extend(postings)
    if all_postings:
        print("  Пример первого отправления (сырые поля):")
        print(json.dumps(all_postings[0], ensure_ascii=False, indent=2)[:2000])
    else:
        print("  Нет отправлений за выбранный период — проверьте POSTING_LOOKBACK_DAYS "
              "в .env или подождите реальный заказ.")
    return all_postings


def ozon_client_lookback():
    import datetime
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    now = datetime.datetime.now(datetime.timezone.utc)
    since = now - datetime.timedelta(days=config.POSTING_LOOKBACK_DAYS)
    return since.strftime(fmt), now.strftime(fmt)


async def step2_product_images(shop, postings):
    print("\n=== 2. Главное фото товара ===")
    offer_ids = []
    for p in postings:
        for item in p.get("products") or []:
            if item.get("offer_id"):
                offer_ids.append(item["offer_id"])
    offer_ids = list(dict.fromkeys(offer_ids))[:5]
    if not offer_ids:
        print("  Нет offer_id для проверки (нет отправлений на шаге 1).")
        return
    images = await ozon_client.get_product_main_images(shop, offer_ids)
    print(f"  Проверено offer_id: {offer_ids}")
    print(f"  Результат: {json.dumps(images, ensure_ascii=False, indent=2)}")
    missing = [oid for oid, url in images.items() if not url]
    if missing:
        print(f"  ВНИМАНИЕ: для {missing} фото не нашлось — проверьте имя поля "
              f"в ozon_client.get_product_main_images (сейчас primary_image/images).")


async def step3_single_label(shop, postings):
    print("\n=== 3. Одна этикетка — реальный размер страницы ===")
    ready = [p["posting_number"] for p in postings if p.get("status") == "awaiting_deliver"]
    if not ready:
        print("  Нет отправлений в статусе awaiting_deliver — пропускаем шаг 3-6.")
        return None
    pn = ready[0]
    ok, payload = await ozon_client.get_label_pdf(shop, [pn])
    if not ok:
        print(f"  Ошибка для {pn}: {payload}")
        return ready
    _save(f"single_{pn}.pdf", payload)
    pages = pdf_label.get_page_count(payload)
    print(f"  {pn}: страниц = {pages}")
    import pypdf, io
    reader = pypdf.PdfReader(io.BytesIO(payload))
    for i, page in enumerate(reader.pages):
        print(f"    страница {i}: mediabox = {page.mediabox} "
              f"({float(page.mediabox.width) / 2.834645:.1f} x {float(page.mediabox.height) / 2.834645:.1f} мм)")
    return ready


async def step4_batch_order(shop, ready):
    print("\n=== 4. Батч из нескольких отправлений — порядок страниц ===")
    if not ready or len(ready) < 2:
        print("  Недостаточно отправлений в awaiting_deliver для проверки батча (нужно >=2).")
        return
    batch = ready[:5]
    ok, payload = await ozon_client.get_label_pdf(shop, batch)
    if not ok:
        print(f"  Ошибка батча: {payload}")
        return
    _save(f"batch_shop{shop.key}.pdf", payload)
    pages = pdf_label.get_page_count(payload)
    print(f"  Запрошено posting_number (в этом порядке): {batch}")
    print(f"  Страниц в ответе: {pages} (ожидали {len(batch)}, если 1 страница = 1 отправление)")
    if pages == len(batch):
        print("  Похоже на простую схему 1 страница = 1 posting_number по порядку запроса.")
        print(f"  ВРУЧНУЮ сверьте scripts/output/batch_shop{shop.key}.pdf: откройте и убедитесь, "
              f"что номер отправления на странице N совпадает с batch[N] выше.")
    else:
        print("  Число страниц НЕ совпало 1:1 — вероятно, есть многоместные отправления. "
              "Пайплайн (pdf_label.fetch_labels) в этом случае автоматически "
              f"откатывается на поштучные вызовы, так что это не баг, но сверьте "
              f"batch_shop{shop.key}.pdf вручную, чтобы понять реальную раскладку.")


async def step5_invalid_in_batch(shop, ready):
    print("\n=== 5. Невалидный posting_number в батче — форма ошибки ===")
    if not ready:
        print("  Пропускаем — нет валидных отправлений для сравнения.")
        return
    batch = ready[:1] + ["00000000-0000-999"]
    ok, payload = await ozon_client.get_label_pdf(shop, batch)
    print(f"  Батч: {batch}")
    print(f"  Успех: {ok}")
    print(f"  Ответ: {json.dumps(payload, ensure_ascii=False, indent=2) if isinstance(payload, dict) else payload}")
    print("  Сверьте с ozon_client.parse_ozon_error — при необходимости уточните "
          "логику различения 'транспортная ошибка' vs 'содержательный отказ' в "
          "pdf_label._bisect_fetch.")


async def step6_label_after_awaiting_deliver(shop, postings):
    print("\n=== 6. (best-effort) Работает ли package-label вне awaiting_deliver ===")
    other = [p["posting_number"] for p in postings if p.get("status") not in ("awaiting_deliver",)]
    if not other:
        print("  Нет отправлений в другом статусе для проверки — пропускаем.")
        return
    pn = other[0]
    ok, payload = await ozon_client.get_label_pdf(shop, [pn])
    print(f"  {pn} (статус вне awaiting_deliver): успех={ok}")
    if not ok:
        print(f"  Ответ: {payload}")
        print("  Значит, для 'старых' отправлений (ушедших дальше по статусам) кэш "
              "label_pdf_cached в БД действительно обязателен, не просто оптимизация.")


async def verify_shop(shop):
    print(f"\n{'#' * 60}\n# Магазин: {shop.name} (SHOP_{shop.key}_*, Client-Id={shop.client_id})\n{'#' * 60}")
    postings = await step1_list_postings(shop)
    await step2_product_images(shop, postings)
    ready = await step3_single_label(shop, postings)
    if ready:
        await step4_batch_order(shop, ready)
        await step5_invalid_in_batch(shop, ready)
        await step6_label_after_awaiting_deliver(shop, postings)


async def main():
    print(f"Настроено магазинов: {len(config.SHOPS)} — {[s.name for s in config.SHOPS]}")
    for shop in config.SHOPS:
        await verify_shop(shop)
    print("\nГотово. Проверьте файлы в scripts/output/ глазами (Preview.app) и "
          "сверьте вывод выше с комментариями 'ПРОВЕРИТЬ' в ozon_client.py/pdf_label.py.")


if __name__ == "__main__":
    asyncio.run(main())
