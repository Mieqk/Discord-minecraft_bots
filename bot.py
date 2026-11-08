"""Точка входа: подключает все модули и запускает бота."""
import os
import logging
from dotenv import load_dotenv

load_dotenv()  # до импорта остальных модулей: store читает DATA_DIR из окружения при импорте

import store
import core, tickets, forms, stats, jobs, commands  # noqa: F401,E402  (регистрируют команды и события)
from core import bot, setup_logging

log = logging.getLogger("bot")


@bot.event
async def on_ready():
    log.info("Вошёл как %s", bot.user)


def main():
    setup_logging()
    store.init(legacy_dir=".")
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit("Не найден DISCORD_TOKEN. Добавь его в переменные окружения хоста "
                         "или в файл .env рядом с bot.py (строка DISCORD_TOKEN=токен).")
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
