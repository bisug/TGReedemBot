from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable


def _split_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


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
    database_url: str = "sqlite+aiosqlite:///./data/bot.db"
    required_channel_ids: tuple[int | str, ...] = ()
    referral_reward_points: int = 1
    withdraw_cost_points: int = 5
    support_stars_amount: int = 10

    @classmethod
    def from_env(cls, *, require_bot_token: bool = True) -> "Settings":
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        if require_bot_token and not bot_token:
            raise ValueError("BOT_TOKEN is required")

        bot_username = os.getenv("BOT_USERNAME", "").strip().lstrip("@")

        return cls(
            bot_token=bot_token,
            admin_ids=_parse_ints(os.getenv("ADMIN_IDS")),
            bot_username=bot_username,
            database_url=os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/bot.db").strip(),
            required_channel_ids=_parse_channel_ids(os.getenv("REQUIRED_CHANNEL_IDS")),
            referral_reward_points=_env_int("REFERRAL_REWARD_POINTS", 1),
            withdraw_cost_points=_env_int("WITHDRAW_COST_POINTS", 5),
            support_stars_amount=_env_int("SUPPORT_STARS_AMOUNT", 10),
        )

    def is_admin(self, telegram_id: int | None) -> bool:
        return telegram_id is not None and telegram_id in self.admin_ids

    def public_channel_links(self) -> Iterable[tuple[str, str]]:
        for channel in self.required_channel_ids:
            if isinstance(channel, str) and channel.startswith("@"):
                username = channel[1:]
                yield channel, f"https://t.me/{username}"
