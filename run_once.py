#!/usr/bin/env python3
"""Разовый проход для запуска по расписанию (GitHub Actions cron и т.п.),
в отличие от bot.py — не крутится бесконечно, а один раз:
  1) опрашивает все магазины (новые заказы, готовые этикетки — poll_once);
  2) забирает и обрабатывает всё, что накопилось в Telegram с прошлого
     запуска (команды, нажатия кнопок);
и завершается.

Состояние (bot_state.sqlite3 — что уже отправлено/напечатано, смещение
Telegram-апдейтов) должно сохраняться МЕЖДУ запусками отдельно — в GitHub
Actions это делает шаг actions/cache в .github/workflows/poll.yml. Без
этого при каждом запуске бот будет считать все заказы новыми.

Интерактивные сессии /merge (какие галочки отмечены) не переживают между
запусками — это ожидаемое ограничение разового режима, см. README/DEPLOY.md.
"""
import asyncio

import config
import db as db_module
import bot


async def main():
    db = await db_module.init_db()
    lock = asyncio.Lock()
    merge_sessions = {}

    print(f"[run_once] Поллинг {len(config.SHOPS)} магазинов: "
          f"{', '.join(s.name for s in config.SHOPS)}")
    await bot.poll_once(db, lock, config.BOT_TOKEN, config.NOTIFY_CHAT_IDS)

    saved_offset = await db_module.kv_get(db, lock, "tg_offset")
    offset = int(saved_offset) + 1 if saved_offset else None

    print("[run_once] Забираю накопленные команды Telegram...")
    processed = 0
    while True:
        resp = await bot.telegram_get_updates(config.BOT_TOKEN, offset, timeout=0)
        updates = resp.get("result", [])
        if not updates:
            break
        for update in updates:
            offset = update["update_id"] + 1
            await db_module.kv_set(db, lock, "tg_offset", offset)
            try:
                message = update.get("message")
                callback_query = update.get("callback_query")
                if message and "text" in message:
                    chat_id = str(message["chat"]["id"])
                    if chat_id in config.ADMIN_CHAT_IDS:
                        await bot.handle_command(db, lock, config.BOT_TOKEN, chat_id,
                                                  message["text"], merge_sessions)
                        processed += 1
                elif callback_query:
                    chat_id = str(callback_query["message"]["chat"]["id"])
                    if chat_id in config.ADMIN_CHAT_IDS:
                        await bot.handle_callback(db, lock, config.BOT_TOKEN, chat_id,
                                                   callback_query, merge_sessions)
                        processed += 1
            except Exception as e:
                print(f"[run_once] обработка апдейта упала: {type(e).__name__}: {e}")

    print(f"[run_once] Обработано команд/нажатий: {processed}. Готово.")
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
