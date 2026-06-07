from __future__ import annotations

import asyncio
from time import monotonic

from telegram import Bot, Update
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from src.config import Settings
from src.database import Database

MembershipCache = dict[tuple[int, tuple[int | str, ...]], tuple[float, bool]]


def get_settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.bot_data["settings"]


def get_database(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.bot_data["database"]


def get_membership_cache(context: ContextTypes.DEFAULT_TYPE) -> MembershipCache:
    cache = context.bot_data.get("membership_cache")
    if not isinstance(cache, dict):
        cache = {}
        context.bot_data["membership_cache"] = cache
    return cache


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


async def has_required_channels(
    bot: Bot,
    settings: Settings,
    telegram_id: int,
    *,
    cache: MembershipCache | None = None,
) -> bool:
    channel_ids = settings.required_channel_ids
    if not channel_ids:
        return True

    cache_key = (telegram_id, channel_ids)
    now = monotonic()
    if cache is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            expires_at, cached_ok = cached
            if expires_at > now:
                return cached_ok
            cache.pop(cache_key, None)

    accepted = {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    }
    checks = await asyncio.gather(
        *(_has_required_channel(bot, channel_id, telegram_id, accepted) for channel_id in channel_ids)
    )
    ok = all(checks)
    if cache is not None:
        _store_membership_cache(cache, cache_key, ok, now, settings)
    return ok


async def _has_required_channel(
    bot: Bot,
    channel_id: int | str,
    telegram_id: int,
    accepted: set[ChatMemberStatus],
) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=telegram_id)
    except TelegramError:
        return False
    if member.status in accepted:
        return True
    return member.status == ChatMemberStatus.RESTRICTED and getattr(member, "is_member", False)


def _store_membership_cache(
    cache: MembershipCache,
    cache_key: tuple[int, tuple[int | str, ...]],
    ok: bool,
    now: float,
    settings: Settings,
) -> None:
    max_entries = settings.membership_cache_max_entries
    if max_entries <= 0:
        return

    ttl = settings.membership_cache_ttl_seconds if ok else settings.membership_cache_negative_ttl_seconds
    if ttl <= 0:
        return

    if len(cache) >= max_entries:
        expired_keys = [key for key, (expires_at, _ok) in cache.items() if expires_at <= now]
        for key in expired_keys:
            cache.pop(key, None)
        if len(cache) >= max_entries:
            cache.clear()

    cache[cache_key] = (now + ttl, ok)


async def answer_callback(update: Update) -> None:
    if update.callback_query:
        await update.callback_query.answer()
