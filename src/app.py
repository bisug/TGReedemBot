from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import replace
from time import monotonic

from telegram.error import Conflict, TelegramError
from telegram.ext import Application, ApplicationBuilder, ContextTypes

from src.config import Settings
from src.core.settings import RequiredChannel
from src.database import Database
from src.modules.admin import register_admin_handlers
from src.modules.payments import register_payment_handlers
from src.modules.user import register_user_handlers

logger = logging.getLogger(__name__)

POLLING_ALLOWED_UPDATES = ("message", "callback_query", "pre_checkout_query")


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
    application.add_error_handler(_log_update_error)
    return application


def _startup(database: Database):
    async def post_init(application: Application) -> None:
        settings: Settings = application.bot_data["settings"]
        logger.info("Starting Redeem Bot lifecycle startup.")
        try:
            await database.acquire_lifecycle_lock(
                _polling_lock_id(settings),
                wait_seconds=settings.startup_lock_wait_seconds,
                retry_seconds=settings.startup_lock_retry_seconds,
            )
            logger.info("Acquired bot lifecycle lock.")
            await database.init_models()
            await _load_bot_identity(application)
            await _load_required_channel_invites(application)
            await _wait_for_polling_slot(application)
            logger.info("Redeem Bot startup completed.")
        except Exception:
            await database.release_lifecycle_lock()
            raise

    return post_init


def _polling_lock_id(settings: Settings) -> int:
    digest = hashlib.blake2b(
        settings.bot_token.encode("utf-8"),
        digest_size=8,
        person=b"redeembot",
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


async def _wait_for_polling_slot(application: Application) -> None:
    settings: Settings = application.bot_data["settings"]
    deadline = monotonic() + settings.polling_conflict_wait_seconds

    while True:
        try:
            await application.bot.get_updates(
                limit=1,
                timeout=0,
                allowed_updates=POLLING_ALLOWED_UPDATES,
            )
            return
        except Conflict as exc:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    "Telegram polling is still locked by another getUpdates request. "
                    "Make sure only one Render worker is running this bot."
                ) from exc

            delay = min(settings.polling_conflict_retry_seconds, remaining)
            logger.warning(
                "Another Telegram getUpdates request is active. Waiting %.1f seconds before retrying startup.",
                delay,
            )
            await asyncio.sleep(delay)
        except TelegramError as exc:
            logger.warning("Could not preflight Telegram polling slot. Continuing startup. Telegram error: %s", exc)
            return


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


async def _load_required_channel_invite(
    application: Application,
    channel: RequiredChannel,
) -> tuple[str | None, str | None]:
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
        logger.info("Shutting down Redeem Bot and releasing database resources.")
        await database.dispose()
        logger.info("Redeem Bot shutdown completed.")

    return post_shutdown


async def _log_update_error(_update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.error is None:
        logger.error("Unhandled bot update error with no exception details.")
        return

    logger.error(
        "Unhandled bot update error.",
        exc_info=(type(context.error), context.error, context.error.__traceback__),
    )
