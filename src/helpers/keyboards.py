from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.config import Settings
from src.utils.ui import Emoji, styled_button


def dashboard_keyboard(*, is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            styled_button(
                "Withdraw",
                callback_data="dashboard:withdraw",
                emoji_id=Emoji.DOWNLOAD,
                style="primary",
            ),
            styled_button(
                "Referral",
                callback_data="dashboard:referral",
                emoji_id=Emoji.GLOBE,
                style="success",
            ),
        ],
        [
            styled_button(
                "Help",
                callback_data="dashboard:help",
                emoji_id=Emoji.FOLDER,
                style="primary",
            ),
            styled_button(
                "Commands",
                callback_data="dashboard:commands",
                emoji_id=Emoji.MENU,
                style="primary",
            ),
        ],
        [
            styled_button(
                "Support Developer",
                callback_data="dashboard:support",
                emoji_id=Emoji.PHONE,
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
                    emoji_id=Emoji.FOLDER_ALT,
                    style="primary",
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def commands_keyboard(*, is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [styled_button("User Commands", callback_data="commands:user", emoji_id=Emoji.FOLDER, style="primary")]
    ]
    if is_admin:
        rows.append(
            [styled_button("Admin Commands", callback_data="commands:admin", emoji_id=Emoji.FOLDER_ALT, style="primary")]
        )
    rows.append([styled_button("Back", callback_data="dashboard:home", emoji_id=Emoji.BACK, style="primary")])
    return InlineKeyboardMarkup(rows)


def verification_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for label, url in settings.public_channel_links():
        rows.append([styled_button(f"Join {label}", url=url, emoji_id=Emoji.GLOBE, style="primary")])
    rows.append([styled_button("I joined", callback_data="verify", emoji_id=Emoji.CHECK, style="success")])
    return InlineKeyboardMarkup(rows)


def back_keyboard(callback_data: str = "dashboard:home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[styled_button("Back", callback_data=callback_data, emoji_id=Emoji.BACK, style="primary")]]
    )
