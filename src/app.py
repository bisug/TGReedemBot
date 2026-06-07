from __future__ import annotations

from telegram.ext import Application, ApplicationBuilder

from src.admin import register_admin_handlers
from src.db import Database
from src.handlers import register_user_handlers
from src.payments import register_payment_handlers
from src.settings import Settings


def build_application(
    settings: Settings,
    database: Database,
    *,
    manage_database_lifecycle: bool = False,
) -> Application:
    builder = ApplicationBuilder().token(settings.bot_token)

    if manage_database_lifecycle:
        builder = builder.post_init(_init_database(database)).post_shutdown(_dispose_database(database))

    application = builder.build()
    application.bot_data["settings"] = settings
    application.bot_data["database"] = database

    register_user_handlers(application)
    register_admin_handlers(application)
    register_payment_handlers(application)
    return application


def _init_database(database: Database):
    async def post_init(_application: Application) -> None:
        await database.init_models()

    return post_init


def _dispose_database(database: Database):
    async def post_shutdown(_application: Application) -> None:
        await database.dispose()

    return post_shutdown
