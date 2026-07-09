#!/usr/bin/env python3
"""Разовый скрипт: шлёт ОДНО сообщение с инлайн-кнопкой web_app на URL мини-
аппа (локальный туннель cloudflared/ngrok на время разработки — см. план
"Telegram Mini App"). Не запускает поллинг Ozon и не трогает getUpdates —
полностью безопасно гонять параллельно с боевым bot.py на VPS, никаких
дублей уведомлений вызвать не может.

Использование (URL туннеля меняется при каждом перезапуске cloudflared):
    python3 scripts/send_webapp_test_button.py https://xxxx.trycloudflare.com

Шлёт кнопку всем, кто перечислен в config.ADMIN_CHAT_IDS (те же люди, что
получают уведомления/могут управлять ботом).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot  # noqa: E402  (нужен .env рядом с bot.py)
import config  # noqa: E402


async def main():
    if len(sys.argv) != 2 or not sys.argv[1].startswith("https://"):
        sys.exit(f"Использование: {sys.argv[0]} <https://...-url-туннеля>")
    url = sys.argv[1]

    keyboard = {"inline_keyboard": [[{"text": "🗂 Открыть список заказов", "web_app": {"url": url}}]]}
    for chat_id in config.NOTIFY_CHAT_IDS:
        await bot.telegram_send_keyboard(config.BOT_TOKEN, chat_id, "Тест мини-аппа:", keyboard)
        print(f"[test] отправлено {chat_id}")


if __name__ == "__main__":
    asyncio.run(main())
