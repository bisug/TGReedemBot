from __future__ import annotations

from telegram import Bot, Update
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from src.config import Settings
from src.database import Database


def get_settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.bot_data["settings"]


def get_database(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.bot_data["database"]


def parse_referral_arg(args: list[str]) -> int | None:
    if not args:
        return None
    first = args[0].strip()
    if not first.startswith("ref_"):
        return None
    raw_id = first.removeprefix("ref_")
    if not raw_id.isdigit():
        return None
    return int(raw_id)


async def has_required_channels(bot: Bot, settings: Settings, telegram_id: int) -> bool:
    if not settings.required_channel_ids:
        return True

    accepted = {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    }
    for channel_id in settings.required_channel_ids:
        try:
            member = await bot.get_chat_member(chat_id=channel_id, user_id=telegram_id)
        except TelegramError:
            return False
        if member.status in accepted:
            continue
        if member.status == ChatMemberStatus.RESTRICTED and getattr(member, "is_member", False):
            continue
        return False
    return True


async def answer_callback(update: Update) -> None:
    if update.callback_query:
        await update.callback_query.answer()
