from __future__ import annotations

from telegram import Update

from src.app import build_application
from src.db import Database
from src.settings import Settings


def main() -> None:
    settings = Settings.from_env()
    database = Database(settings.database_url)
    application = build_application(settings, database, manage_database_lifecycle=True)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
