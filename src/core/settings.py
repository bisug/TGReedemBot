from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable

from dotenv import load_dotenv

load_dotenv()


def _split_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in re.split(r"[\s,;]+", value) if part.strip())


def _parse_ints(value: str | None) -> frozenset[int]:
    ids: set[int] = set()
    for part in _split_csv(value):
        try:
            ids.add(int(part))
        except ValueError as exc:
            raise ValueError(f"Invalid integer ID in ADMIN_IDS: {part!r}") from exc
    return frozenset(ids)


def _parse_channel_ids(value: str | None) -> tuple[int | str, ...]:
    channels: list[int | str] = []
    for part in _split_csv(value):
        if part.lstrip("-").isdigit():
            channels.append(int(part))
        else:
            channels.append(part)
    return tuple(channels)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def normalize_database_url(database_url: str) -> str:
    value = database_url.strip()
    if not value:
        raise ValueError("DATABASE_URL is required")
    if value.startswith("postgres://"):
        return "postgresql+asyncpg://" + value.removeprefix("postgres://")
    if value.startswith("postgresql://"):
        return "postgresql+asyncpg://" + value.removeprefix("postgresql://")
    if value.startswith("postgres+asyncpg://"):
        return "postgresql+asyncpg://" + value.removeprefix("postgres+asyncpg://")
    if value.startswith("postgresql+asyncpg://"):
        return value
    raise ValueError("DATABASE_URL must be a PostgreSQL URL")


@dataclass(frozen=True, slots=True)
class RequiredChannel:
    chat_id: int | str
    label: str
    join_url: str | None


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    admin_ids: frozenset[int]
    bot_username: str = ""
    bot_id: int | None = None
    bot_name: str = ""
    bot_can_join_groups: bool | None = None
    bot_can_read_all_group_messages: bool | None = None
    bot_supports_inline_queries: bool | None = None
    database_url: str = ""
    required_channel_ids: tuple[int | str, ...] = ()
    required_channel_generated_links: tuple[str | None, ...] = ()
    required_channel_generated_labels: tuple[str | None, ...] = ()
    referral_reward_points: int = 1
    withdraw_cost_points: int = 5
    support_stars_amount: int = 10
    telegram_concurrent_updates: int = 64
    telegram_connection_pool_size: int = 64
    telegram_pool_timeout_seconds: int = 10
    database_pool_size: int = 10
    database_max_overflow: int = 20
    database_pool_timeout_seconds: int = 30
    database_pool_pre_ping: bool = False
    membership_cache_ttl_seconds: int = 300
    membership_cache_negative_ttl_seconds: int = 15
    membership_cache_max_entries: int = 10000
    broadcast_concurrency: int = 20
    startup_lock_wait_seconds: int = 180
    startup_lock_retry_seconds: int = 2
    polling_conflict_wait_seconds: int = 180
    polling_conflict_retry_seconds: int = 5

    @classmethod
    def from_env(cls, *, require_bot_token: bool = True) -> "Settings":
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        if require_bot_token and not bot_token:
            raise ValueError("BOT_TOKEN is required")

        bot_username = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
        database_url = normalize_database_url(os.getenv("DATABASE_URL", ""))

        return cls(
            bot_token=bot_token,
            admin_ids=_parse_ints(os.getenv("ADMIN_IDS")),
            bot_username=bot_username,
            database_url=database_url,
            required_channel_ids=_parse_channel_ids(os.getenv("REQUIRED_CHANNEL_IDS")),
            referral_reward_points=_env_int("REFERRAL_REWARD_POINTS", 1),
            withdraw_cost_points=_env_int("WITHDRAW_COST_POINTS", 5),
            support_stars_amount=_env_int("SUPPORT_STARS_AMOUNT", 10),
            telegram_concurrent_updates=max(1, _env_int("TELEGRAM_CONCURRENT_UPDATES", 64)),
            telegram_connection_pool_size=max(1, _env_int("TELEGRAM_CONNECTION_POOL_SIZE", 64)),
            telegram_pool_timeout_seconds=max(1, _env_int("TELEGRAM_POOL_TIMEOUT_SECONDS", 10)),
            database_pool_size=max(1, _env_int("DATABASE_POOL_SIZE", 10)),
            database_max_overflow=max(0, _env_int("DATABASE_MAX_OVERFLOW", 20)),
            database_pool_timeout_seconds=max(1, _env_int("DATABASE_POOL_TIMEOUT_SECONDS", 30)),
            database_pool_pre_ping=_env_bool("DATABASE_POOL_PRE_PING", False),
            membership_cache_ttl_seconds=max(0, _env_int("MEMBERSHIP_CACHE_TTL_SECONDS", 300)),
            membership_cache_negative_ttl_seconds=max(
                0, _env_int("MEMBERSHIP_CACHE_NEGATIVE_TTL_SECONDS", 15)
            ),
            membership_cache_max_entries=max(0, _env_int("MEMBERSHIP_CACHE_MAX_ENTRIES", 10000)),
            broadcast_concurrency=max(1, _env_int("BROADCAST_CONCURRENCY", 20)),
            startup_lock_wait_seconds=max(1, _env_int("STARTUP_LOCK_WAIT_SECONDS", 180)),
            startup_lock_retry_seconds=max(1, _env_int("STARTUP_LOCK_RETRY_SECONDS", 2)),
            polling_conflict_wait_seconds=max(1, _env_int("POLLING_CONFLICT_WAIT_SECONDS", 180)),
            polling_conflict_retry_seconds=max(1, _env_int("POLLING_CONFLICT_RETRY_SECONDS", 5)),
        )

    def is_admin(self, telegram_id: int | None) -> bool:
        return telegram_id is not None and telegram_id in self.admin_ids

    def public_channel_links(self) -> Iterable[tuple[str, str]]:
        for channel in self.required_channels():
            if channel.join_url:
                yield channel.label, channel.join_url

    def required_channels(self) -> tuple[RequiredChannel, ...]:
        channels: list[RequiredChannel] = []
        for index, channel in enumerate(self.required_channel_ids):
            generated_url = (
                self.required_channel_generated_links[index]
                if index < len(self.required_channel_generated_links)
                else None
            )
            generated_label = (
                self.required_channel_generated_labels[index]
                if index < len(self.required_channel_generated_labels)
                else None
            )
            join_url = generated_url or None
            if isinstance(channel, str) and channel.startswith("@"):
                label = generated_label or channel
                join_url = join_url or f"https://t.me/{channel[1:]}"
            else:
                label = generated_label or f"Channel {index + 1}"
            channels.append(RequiredChannel(chat_id=channel, label=label, join_url=join_url))
        return tuple(channels)
