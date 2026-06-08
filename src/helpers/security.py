from __future__ import annotations

from time import monotonic

from telegram import Update
from telegram.ext import ContextTypes

RateLimitCache = dict[tuple[int, str], tuple[float, int]]

MAX_RATE_LIMIT_ENTRIES = 20000


def get_rate_limit_cache(context: ContextTypes.DEFAULT_TYPE) -> RateLimitCache:
    cache = context.bot_data.get("rate_limit_cache")
    if not isinstance(cache, dict):
        cache = {}
        context.bot_data["rate_limit_cache"] = cache
    return cache


async def require_private_chat(update: Update) -> bool:
    chat = update.effective_chat
    if chat is None or chat.type == "private":
        return True

    if update.callback_query:
        await update.callback_query.answer("Use this bot in private chat.", show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text("Please use this bot in a private chat.")
    return False


async def enforce_rate_limit(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    bucket: str,
    *,
    limit: int,
    window_seconds: int,
) -> bool:
    user = update.effective_user
    if user is None:
        return True

    cache = get_rate_limit_cache(context)
    now = monotonic()
    key = (user.id, bucket)
    window_start, count = cache.get(key, (now, 0))
    if now - window_start >= window_seconds:
        window_start = now
        count = 0

    count += 1
    cache[key] = (window_start, count)
    if len(cache) > MAX_RATE_LIMIT_ENTRIES:
        _prune_rate_limit_cache(cache, now)

    if count <= limit:
        return True

    await _send_rate_limit_notice(update)
    return False


def _prune_rate_limit_cache(cache: RateLimitCache, now: float) -> None:
    expired_keys = [
        key
        for key, (window_start, _count) in cache.items()
        if now - window_start >= 300
    ]
    for key in expired_keys:
        cache.pop(key, None)
    if len(cache) > MAX_RATE_LIMIT_ENTRIES:
        cache.clear()


async def _send_rate_limit_notice(update: Update) -> None:
    message = "Please slow down and try again in a moment."
    if update.callback_query:
        await update.callback_query.answer(message, show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(message)
