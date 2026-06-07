from __future__ import annotations

import logging
from dataclasses import replace

from telegram.error import TelegramError
from telegram.ext import Application, ApplicationBuilder

from src.config import Settings
from src.database import Database
from src.modules.admin import register_admin_handlers
from src.modules.payments import register_payment_handlers
from src.modules.user import register_user_handlers

logger = logging.getLogger(__name__)


def build_application(
    settings: Settings,
    database: Database,
    *,
    manage_database_lifecycle: bool = False,
) -> Application:
    builder = ApplicationBuilder().token(settings.bot_token)

    if manage_database_lifecycle:
        builder = builder.post_init(_startup(database)).post_shutdown(_dispose_database(database))

    application = builder.build()
    application.bot_data["settings"] = settings
    application.bot_data["database"] = database

    register_user_handlers(application)
    register_admin_handlers(application)
    register_payment_handlers(application)
    return application


def _startup(database: Database):
    async def post_init(_application: Application) -> None:
        await database.init_models()
        await _load_bot_identity(_application)
        await _load_required_channel_invites(_application)

    return post_init


async def _load_bot_identity(application: Application) -> None:
    settings: Settings = application.bot_data["settings"]
    bot_user = await application.bot.get_me()
    application.bot_data["bot_info"] = bot_user
    application.bot_data["settings"] = replace(
        settings,
        bot_id=bot_user.id,
        bot_username=bot_user.username or settings.bot_username,
        bot_name=bot_user.first_name or settings.bot_name,
        bot_can_join_groups=getattr(bot_user, "can_join_groups", None),
        bot_can_read_all_group_messages=getattr(bot_user, "can_read_all_group_messages", None),
        bot_supports_inline_queries=getattr(bot_user, "supports_inline_queries", None),
    )


async def _load_required_channel_invites(application: Application) -> None:
    settings: Settings = application.bot_data["settings"]
    generated_links: list[str | None] = []

    for channel in settings.required_channels():
        if channel.join_url:
            generated_links.append(None)
            continue

        try:
            invite = await application.bot.create_chat_invite_link(
                chat_id=channel.chat_id,
                name="Redeem Bot Force Join",
            )
        except TelegramError as exc:
            generated_links.append(None)
            logger.warning(
                "Could not create invite link for required chat %r. "
                "The bot must be an admin with invite-user permission. Telegram error: %s",
                channel.chat_id,
                exc,
            )
            continue

        generated_links.append(invite.invite_link)

    application.bot_data["settings"] = replace(
        settings,
        required_channel_generated_links=tuple(generated_links),
    )


def _dispose_database(database: Database):
    async def post_shutdown(_application: Application) -> None:
        await database.dispose()

    return post_shutdown
