from __future__ import annotations

from src.app import build_application
from src.config import Settings
from src.database import Database

FAST_ALLOWED_UPDATES = ("message", "callback_query", "pre_checkout_query")


def main() -> None:
    settings = Settings.from_env()
    database = Database(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout_seconds,
        pool_pre_ping=settings.database_pool_pre_ping,
    )
    application = build_application(settings, database, manage_database_lifecycle=True)
    application.run_polling(allowed_updates=FAST_ALLOWED_UPDATES)


if __name__ == "__main__":
    main()
