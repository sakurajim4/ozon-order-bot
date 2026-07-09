"""PDF-«опознавашка» для сборщика — НЕ этикетка Ozon (см. pdf_label.py),
отдельный, чисто визуальный документ: сеткой 2×3 на A4, в каждой ячейке
фото товара и его название крупным текстом снизу. Собирается по выбору
пользователя в Telegram Mini App (webapp_server.py) из уже загруженных
фото (или отсутствующих — placeholder, сборка никогда не падает из-за
одного отсутствующего фото).

Ничего не пишет в БД и не помечает отправления напечатанными — это
read-only просмотровая фича, полностью независимая от printed_at/
label_sent_at (тех же полей, которыми управляет /merge в bot.py).
"""
import io

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

import pdf_label

FONT_NAME = pdf_label.FONT_NAME  # уже зарегистрирован (кириллица), не дублируем

PAGE_W, PAGE_H = A4
COLS, ROWS = 2, 3
MARGIN = 8 * mm
GAP = 4 * mm
CELL_PAD = 3 * mm
PHOTO_RATIO = 0.66  # доля высоты ячейки, отданная под фото (остальное — под текст)

CELL_W = (PAGE_W - 2 * MARGIN - (COLS - 1) * GAP) / COLS
CELL_H = (PAGE_H - 2 * MARGIN - (ROWS - 1) * GAP) / ROWS

MIN_FONT_SIZE = 7
MAX_FONT_SIZE = 15


def _wrap_text(c: canvas.Canvas, text: str, font_size: float, max_width: float) -> list:
    words = text.split()
    if not words:
        return [""]
    lines = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if c.stringWidth(candidate, FONT_NAME, font_size) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _fit_multiline(c: canvas.Canvas, text: str, max_width: float, max_height: float) -> tuple:
    """Подбирает наибольший размер шрифта (MIN..MAX), при котором перенесённый
    по словам текст влезает в max_width×max_height; если даже минимальный не
    влезает — обрезает последнюю влезающую строку многоточием (название
    никогда не скрывается полностью, как и в pdf_label._fit_text)."""
    size = MAX_FONT_SIZE
    while size >= MIN_FONT_SIZE:
        lines = _wrap_text(c, text, size, max_width)
        line_height = size * 1.2
        if line_height * len(lines) <= max_height:
            return lines, size
        size -= 0.5

    size = MIN_FONT_SIZE
    lines = _wrap_text(c, text, size, max_width)
    line_height = size * 1.2
    max_lines = max(1, int(max_height // line_height))
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while len(last) > 1 and c.stringWidth(last + "…", FONT_NAME, size) > max_width:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines, size


def _draw_photo(c: canvas.Canvas, photo_bytes, x: float, y: float, w: float, h: float) -> None:
    if photo_bytes:
        try:
            img = ImageReader(io.BytesIO(photo_bytes))
            iw, ih = img.getSize()
            scale = min(w / iw, h / ih)
            draw_w, draw_h = iw * scale, ih * scale
            c.drawImage(
                img, x + (w - draw_w) / 2, y + (h - draw_h) / 2, width=draw_w, height=draw_h,
                preserveAspectRatio=True, mask="auto",
            )
            return
        except Exception as e:
            print(f"[picking-list] фото не отрисовалось, placeholder: {e}")
    c.setFillColorRGB(0.92, 0.92, 0.92)
    c.rect(x, y, w, h, fill=1, stroke=0)
    c.setFillColorRGB(0.55, 0.55, 0.55)
    c.setFont(FONT_NAME, 9)
    c.drawCentredString(x + w / 2, y + h / 2 - 3, "нет фото")


def _draw_qty_badge(c: canvas.Canvas, qty: int, top_right_x: float, top_right_y: float) -> None:
    if not qty or qty <= 1:
        return
    r = 5 * mm
    cx, cy = top_right_x - r, top_right_y - r
    c.setFillColorRGB(0.1, 0.1, 0.1)
    c.circle(cx, cy, r, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1)
    c.setFont(FONT_NAME, 9)
    c.drawCentredString(cx, cy - 3, f"×{qty}")


def _draw_cell(c: canvas.Canvas, item: dict, cell_x: float, cell_y: float) -> None:
    """cell_y — нижний край ячейки (координаты PDF растут снизу вверх)."""
    c.setStrokeColorRGB(0.85, 0.85, 0.85)
    c.rect(cell_x, cell_y, CELL_W, CELL_H, fill=0, stroke=1)

    photo_h = CELL_H * PHOTO_RATIO
    photo_y = cell_y + CELL_H - photo_h
    _draw_photo(
        c, item.get("photo_bytes"),
        cell_x + CELL_PAD, photo_y + CELL_PAD / 2,
        CELL_W - 2 * CELL_PAD, photo_h - CELL_PAD,
    )
    _draw_qty_badge(c, item.get("qty", 1), cell_x + CELL_W - 2, cell_y + CELL_H - 2)

    name_area_h = CELL_H - photo_h
    max_text_width = CELL_W - 2 * CELL_PAD
    max_text_height = name_area_h - CELL_PAD
    lines, font_size = _fit_multiline(c, item.get("name") or "?", max_text_width, max_text_height)
    line_height = font_size * 1.2
    text_block_h = line_height * len(lines)
    # Блок строк центрируем по вертикали в отведённой под текст области.
    ty = cell_y + name_area_h - CELL_PAD - max(0, (max_text_height - text_block_h) / 2) - font_size * 0.85
    c.setFillColorRGB(0, 0, 0)
    c.setFont(FONT_NAME, font_size)
    for line in lines:
        c.drawCentredString(cell_x + CELL_W / 2, ty, line)
        ty -= line_height


def build_pdf(items: list) -> bytes:
    """items: [{"photo_bytes": bytes|None, "name": str, "qty": int}, ...].
    Порядок в PDF — тот же, что во входном списке. Каждый item — одна
    ячейка сетки 2×3; после ROWS*COLS элементов — новая страница."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    per_page = COLS * ROWS
    for page_start in range(0, len(items), per_page):
        for idx, item in enumerate(items[page_start:page_start + per_page]):
            col, row = idx % COLS, idx // COLS
            cell_x = MARGIN + col * (CELL_W + GAP)
            cell_y = PAGE_H - MARGIN - (row + 1) * CELL_H - row * GAP
            _draw_cell(c, item, cell_x, cell_y)
        c.showPage()
    c.save()
    return buf.getvalue()
