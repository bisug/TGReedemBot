from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.settings import Settings
from src.ui import Emoji, styled_button


def dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
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
                    "Support Developer",
                    callback_data="dashboard:support",
                    emoji_id=Emoji.PHONE,
                    style="primary",
                )
            ],
        ]
    )


def verification_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for label, url in settings.public_channel_links():
        rows.append([styled_button(f"Join {label}", url=url, emoji_id=Emoji.GLOBE, style="primary")])
    rows.append([styled_button("I joined", callback_data="verify", emoji_id=Emoji.CHECK, style="success")])
    return InlineKeyboardMarkup(rows)


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[styled_button("Back", callback_data="dashboard:home", emoji_id=Emoji.BACK, style="primary")]]
    )
