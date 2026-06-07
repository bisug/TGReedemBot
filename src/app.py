from __future__ import annotations

import asyncio
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
    builder = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .concurrent_updates(settings.telegram_concurrent_updates)
        .connection_pool_size(settings.telegram_connection_pool_size)
        .pool_timeout(settings.telegram_pool_timeout_seconds)
    )

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
    channels = settings.required_channels()
    if not channels:
        return

    channel_results = await asyncio.gather(
        *(_load_required_channel_invite(application, channel) for channel in channels)
    )
    generated_links = tuple(link for link, _label in channel_results)
    generated_labels = tuple(label for _link, label in channel_results)

    application.bot_data["settings"] = replace(
        settings,
        required_channel_generated_links=generated_links,
        required_channel_generated_labels=generated_labels,
    )


async def _load_required_channel_invite(application: Application, channel) -> tuple[str | None, str | None]:  # type: ignore[no-untyped-def]
    generated_label: str | None = None
    generated_link: str | None = None

    try:
        chat = await application.bot.get_chat(channel.chat_id)
        generated_label = chat.title or chat.full_name or chat.username or channel.label
    except TelegramError as exc:
        logger.warning(
            "Could not fetch title for required chat %r. Telegram error: %s",
            channel.chat_id,
            exc,
        )

    if not channel.join_url:
        try:
            invite = await application.bot.create_chat_invite_link(
                chat_id=channel.chat_id,
                name="Redeem Bot Force Join",
            )
            generated_link = invite.invite_link
        except TelegramError as exc:
            logger.warning(
                "Could not create invite link for required chat %r. "
                "The bot must be an admin with invite-user permission. Telegram error: %s",
                channel.chat_id,
                exc,
            )

    return generated_link, generated_label


def _dispose_database(database: Database):
    async def post_shutdown(_application: Application) -> None:
        await database.dispose()

    return post_shutdown
