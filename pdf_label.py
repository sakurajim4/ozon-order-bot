"""Пайплайн печати этикеток: батчинг с изоляцией ошибок Ozon, маппинг страниц
PDF на posting_number, наложение артикула снизу, склейка.

Ozon отдаёт /v2/posting/fbs/package-label all-or-nothing (одно неверное
отправление роняет весь батч из <=20) и не документирует порядок страниц в
объединённом PDF. Поэтому здесь два защитных механизма:
  1. бисекция при отказе батча — изолирует ровно тот posting_number, который
     мешает остальным, вместо потери всего чанка;
  2. проверка числа страниц (1 на отправление) после успешного батч-вызова —
     если не совпало (многоместное отправление или сюрприз формата), не
     гадаем, а откатываемся на поштучные вызовы для всего чанка — так
     гарантируется, что артикул никогда не попадёт не на ту этикетку.

Это рабочее предположение о порядке страниц ещё не подтверждено вживую —
см. scripts/verify_label_api_assumptions.py. Пока не проверено, откат на
поштучные вызовы должен срабатывать при малейшем сомнении, а не оптимизм.
"""
import io
import os

import pypdf
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

import config
import ozon_client

# Базовые PDF-шрифты (Helvetica и т.п.) не содержат кириллицу — артикулы и
# подписи на русском рисовались бы битыми глифами. Используем шрифт с
# поддержкой кириллицы, поставляемый вместе с проектом (см. assets/).
FONT_NAME = "OzonLabelFont"
_FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "DejaVuSans-Bold.ttf")
pdfmetrics.registerFont(TTFont(FONT_NAME, _FONT_PATH))

# Размер страницы не меняем — печатаем поверх, мелким шрифтом, в свободном
# кармане у нижнего края. Замер по реальной этикетке (58×40мм, 2026-07):
# в нижних ~6мм высоты чёрный контент есть только в узкой колонке слева
# (вертикальный номер отправления, примерно 8-14мм от левого края) — всё
# правее, от ~15мм и до правого края, в этой полосе пусто (QR и "OZON"
# начинаются выше). Поэтому текст стартует правее этой колонки, а не от
# самого левого края. Если Ozon в очередной раз поменяет вёрстку этикетки —
# отступы ниже придётся перепромерить (см. scripts/verify_label_api_assumptions.py).
MIN_FONT_SIZE = 3.5
MAX_FONT_SIZE = 6
LEFT_MARGIN = 15 * mm   # правее колонки с вертикальным номером отправления
RIGHT_MARGIN = 2 * mm
BOTTOM_MARGIN = 1.5 * mm
LINE_GAP = 1.5  # пт, промежуток между строками сверх высоты шрифта (если строк больше одной)


def format_offer_id_lines(products: list, max_items: int = None) -> list:
    """Одна строка для наложения снизу: голые артикулы продавца через
    запятую (+ количество, если >1), без подписи "Артикул:". Свободный
    карман под текст на этикетке широкий, но невысокий (~6мм) — поэтому все
    товары сводятся в ОДНУ строку, а не по одному на строку: несколько
    строк друг под другом в эту высоту просто не влезут и всё равно заедут
    на QR."""
    if max_items is None:
        max_items = config.MAX_OFFER_IDS_ON_LABEL
    totals = {}
    order = []
    for p in products:
        offer_id = p.get("offer_id") or "?"
        qty = p.get("quantity") or 1
        if offer_id not in totals:
            order.append(offer_id)
        totals[offer_id] = totals.get(offer_id, 0) + qty

    shown = order[:max_items]
    rest = order[max_items:]
    parts = []
    for offer_id in shown:
        qty = totals[offer_id]
        parts.append(f"{offer_id} ×{qty}" if qty > 1 else offer_id)
    if rest:
        parts.append(f"+{len(rest)}")
    return [", ".join(parts)]


def _fit_text(c: canvas.Canvas, text: str, max_width: float) -> tuple:
    """Подбирает наибольший размер шрифта (MIN..MAX), при котором строка
    влезает в max_width; если даже минимальный не влезает — обрезает с
    многоточием (крайняя мера, артикул никогда не скрывается полностью)."""
    font_size = MAX_FONT_SIZE
    while font_size > MIN_FONT_SIZE and c.stringWidth(text, FONT_NAME, font_size) > max_width:
        font_size -= 0.5
    if c.stringWidth(text, FONT_NAME, font_size) > max_width:
        while len(text) > 1 and c.stringWidth(text + "…", FONT_NAME, font_size) > max_width:
            text = text[:-1]
        text = text + "…"
    return text, font_size


def _stamp_page(page, lines: list) -> None:
    width = float(page.mediabox.width)
    height = float(page.mediabox.height)
    max_width = width - LEFT_MARGIN - RIGHT_MARGIN

    overlay_buf = io.BytesIO()
    c = canvas.Canvas(overlay_buf, pagesize=(width, height))
    y = BOTTOM_MARGIN
    for text in reversed(lines):  # первая строка списка ближе к нижнему краю
        fitted, font_size = _fit_text(c, text, max_width)
        text_width = c.stringWidth(fitted, FONT_NAME, font_size)
        pad = 0.8  # компактная подложка — только под сам текст, впритык
        c.setFillColorRGB(1, 1, 1)
        c.rect(LEFT_MARGIN - pad, y - pad, text_width + 2 * pad, font_size + 2 * pad, fill=1, stroke=0)
        c.setFillColorRGB(0, 0, 0)
        c.setFont(FONT_NAME, font_size)
        c.drawString(LEFT_MARGIN, y, fitted)
        y += font_size + LINE_GAP
    c.save()
    overlay_buf.seek(0)

    overlay_page = pypdf.PdfReader(overlay_buf).pages[0]
    page.merge_page(overlay_page)


def overlay_offer_ids(pdf_bytes: bytes, lines: list) -> bytes:
    """Накладывает артикул(ы) поверх нижнего края каждой страницы, не меняя
    размер страницы (для многоместных отправлений — один и тот же список на
    все страницы, см. план). Мелкий шрифт и белая подложка впритык к тексту
    — чтобы задеть как можно меньше уже существующего содержимого этикетки."""
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    writer = pypdf.PdfWriter()
    for page in reader.pages:
        _stamp_page(page, lines)
        writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def get_page_count(pdf_bytes: bytes) -> int:
    return len(pypdf.PdfReader(io.BytesIO(pdf_bytes)).pages)


def _try_split_evenly(pdf_bytes: bytes, posting_numbers: list):
    """Пытается разрезать pdf_bytes ровно по 1 странице на posting_number в
    заданном порядке. None, если число страниц не совпало 1:1 — тогда
    вызывающий код обязан откатиться на поштучные вызовы."""
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    if len(reader.pages) != len(posting_numbers):
        return None
    result = {}
    for pn, page in zip(posting_numbers, reader.pages):
        writer = pypdf.PdfWriter()
        writer.add_page(page)
        buf = io.BytesIO()
        writer.write(buf)
        result[pn] = buf.getvalue()
    return result


async def _bisect_fetch(shop, posting_numbers: list):
    """При отказе батч-вызова делит список пополам рекурсивно, пока не
    изолирует ровно те posting_number, что вызывают отказ. Возвращает
    (список успешных подгрупп [(subset, combined_pdf_bytes)], {posting_number: сообщение_ошибки})."""
    success, payload = await ozon_client.get_label_pdf(shop, posting_numbers)
    if success:
        return [(posting_numbers, payload)], {}
    if len(posting_numbers) == 1:
        return [], {posting_numbers[0]: payload.get("message", "неизвестная ошибка")}
    mid = len(posting_numbers) // 2
    left_ok, left_err = await _bisect_fetch(shop, posting_numbers[:mid])
    right_ok, right_err = await _bisect_fetch(shop, posting_numbers[mid:])
    return left_ok + right_ok, {**left_err, **right_err}


async def fetch_labels(shop, posting_numbers: list) -> tuple:
    """Получает этикетки (без наложения) для всех posting_numbers ОДНОГО
    магазина (все они должны принадлежать shop — вызывающий код группирует
    посылки по shop_key перед вызовом).
    Возвращает (label_map: {posting_number: raw_pdf_bytes}, error_map: {posting_number: reason})."""
    label_map = {}
    error_map = {}
    for i in range(0, len(posting_numbers), config.LABEL_BATCH_SIZE):
        chunk = posting_numbers[i:i + config.LABEL_BATCH_SIZE]
        subgroups, errs = await _bisect_fetch(shop, chunk)
        error_map.update(errs)
        for subset, pdf_bytes in subgroups:
            split = _try_split_evenly(pdf_bytes, subset)
            if split is not None:
                label_map.update(split)
                continue
            # Число страниц не совпало 1:1 (многоместное отправление или
            # непредвиденный формат) — гарантированно верный, но более
            # медленный откат: по одному вызову на posting_number.
            for pn in subset:
                ok, single_payload = await ozon_client.get_label_pdf(shop, [pn])
                if ok:
                    label_map[pn] = single_payload
                else:
                    error_map[pn] = single_payload.get("message", "неизвестная ошибка")
    return label_map, error_map


def merge_pdfs(pdf_bytes_list: list) -> bytes:
    writer = pypdf.PdfWriter()
    for pdf_bytes in pdf_bytes_list:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        for page in reader.pages:
            writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()
