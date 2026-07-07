import os
import sys
from dataclasses import dataclass

from dotenv import load_dotenv

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_BASE_DIR, ".env"), override=True)


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(
            f"[config] Отсутствует обязательная переменная {name}.\n"
            f"Заполните {os.path.join(_BASE_DIR, '.env')} "
            f"(см. .env.example) и запустите бота снова."
        )
    return value


@dataclass(frozen=True)
class Shop:
    key: str        # короткий идентификатор ("1", "2", ...) — хранится в БД
    name: str        # человеко-читаемое имя для подписи в Telegram
    client_id: str
    api_key: str


def _load_shops() -> list:
    """Магазины задаются нумерованными переменными SHOP_1_*, SHOP_2_*, ...
    (см. .env.example) — так можно добавить сколько угодно магазинов, не
    трогая код."""
    shops = []
    i = 1
    while True:
        prefix = f"SHOP_{i}_"
        client_id = os.environ.get(prefix + "CLIENT_ID", "").strip()
        api_key = os.environ.get(prefix + "API_KEY", "").strip()
        if not client_id and not api_key:
            break
        if not client_id or not api_key:
            sys.exit(
                f"[config] {prefix}CLIENT_ID и {prefix}API_KEY должны быть "
                f"заданы вместе (магазин {i}). Проверьте .env."
            )
        name = os.environ.get(prefix + "NAME", f"Магазин {i}").strip()
        shops.append(Shop(key=str(i), name=name, client_id=client_id, api_key=api_key))
        i += 1
    if not shops:
        sys.exit(
            "[config] Не задано ни одного магазина. Заполните SHOP_1_CLIENT_ID "
            "и SHOP_1_API_KEY в .env (см. .env.example)."
        )
    return shops


SHOPS = _load_shops()
BOT_TOKEN = _require("BOT_TOKEN")
CHAT_ID = _require("CHAT_ID")

DB_PATH = os.path.join(_BASE_DIR, "bot_state.sqlite3")

# Как часто опрашивать Ozon на новые/готовые к отгрузке отправления.
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "90"))

# Если бот упал — подождать и перезапустить сам себя (см. run() в bot.py).
AUTO_RESTART_SECONDS = int(os.environ.get("AUTO_RESTART_SECONDS", "3600"))

# Раз в сколько секунд слать эвристический heartbeat (0 = отключить).
HEARTBEAT_SECONDS = int(os.environ.get("HEARTBEAT_SECONDS", str(6 * 3600)))

# Не запрашивать отправления старше этого числа дней при поллинге —
# ограничивает окно пагинации, не влияет на уже сохранённые в БД.
POSTING_LOOKBACK_DAYS = int(os.environ.get("POSTING_LOOKBACK_DAYS", "3"))

# Ozon: не больше 20 posting_number в одном вызове package-label.
LABEL_BATCH_SIZE = 20

# Сколько разных offer_id показывать в строке артикулов на этикетке (все —
# через запятую, в одну строку, см. pdf_label.py), прежде чем свернуть
# остальные в "+N".
MAX_OFFER_IDS_ON_LABEL = int(os.environ.get("MAX_OFFER_IDS_ON_LABEL", "4"))

# Telegram: жёсткий лимит сервера на загружаемый ботом документ — 50 МБ.
TELEGRAM_MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
