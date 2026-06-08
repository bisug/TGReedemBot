from __future__ import annotations

import signal

from src.app import POLLING_ALLOWED_UPDATES, build_application
from src.config import Settings
from src.database import Database

STOP_SIGNALS = tuple(
    getattr(signal, name)
    for name in ("SIGINT", "SIGTERM")
    if hasattr(signal, name)
)


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
    application.run_polling(
        allowed_updates=POLLING_ALLOWED_UPDATES,
        stop_signals=STOP_SIGNALS,
    )


if __name__ == "__main__":
    main()
