from __future__ import annotations

from functools import lru_cache

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.config import Settings
from src.utils.ui import Emoji, styled_button


@lru_cache(maxsize=4)
def dashboard_keyboard(*, is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            styled_button(
                "Withdraw",
                callback_data="dashboard:withdraw",
                emoji_id=Emoji.CREDIT_CARD,
                style="primary",
            ),
            styled_button(
                "Referral",
                callback_data="dashboard:referral",
                emoji_id=Emoji.PEOPLE_HUGGING,
                style="success",
            ),
        ],
        [
            styled_button(
                "Help",
                callback_data="dashboard:help",
                emoji_id=Emoji.INFO,
                style="primary",
            ),
            styled_button(
                "Commands",
                callback_data="dashboard:commands",
                emoji_id=Emoji.CLIPBOARD,
                style="primary",
            ),
        ],
        [
            styled_button(
                "Support Developer",
                callback_data="dashboard:support",
                emoji_id=Emoji.HEART,
                style="primary",
            )
        ],
    ]
    if is_admin:
        rows.append(
            [
                styled_button(
                    "Admin Panel",
                    callback_data="dashboard:admin",
                    emoji_id=Emoji.GEAR,
                    style="primary",
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


@lru_cache(maxsize=4)
def commands_keyboard(*, is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [styled_button("User Commands", callback_data="commands:user", emoji_id=Emoji.DOCUMENT, style="primary")]
    ]
    if is_admin:
        rows.append(
            [styled_button("Admin Commands", callback_data="commands:admin", emoji_id=Emoji.GEAR, style="primary")]
        )
    rows.append([styled_button("Back", callback_data="dashboard:home", emoji_id=Emoji.BACK, style="primary")])
    return InlineKeyboardMarkup(rows)


def verification_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index, channel in enumerate(settings.required_channels(), start=1):
        if channel.join_url:
            button = styled_button(
                f"Join {channel.label}",
                url=channel.join_url,
                emoji_id=Emoji.GLOBE,
                style="primary",
            )
        else:
            button = styled_button(
                f"Join {channel.label}",
                callback_data=f"join:missing:{index}",
                emoji_id=Emoji.GLOBE,
                style="primary",
            )
        rows.append([button])
    rows.append([styled_button("I joined", callback_data="verify", emoji_id=Emoji.SUCCESS, style="success")])
    return InlineKeyboardMarkup(rows)


@lru_cache(maxsize=32)
def back_keyboard(callback_data: str = "dashboard:home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[styled_button("Back", callback_data=callback_data, emoji_id=Emoji.BACK, style="primary")]]
    )
