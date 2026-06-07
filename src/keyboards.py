from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.settings import Settings


def dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Withdraw", callback_data="dashboard:withdraw"),
                InlineKeyboardButton("Referral", callback_data="dashboard:referral"),
            ],
            [InlineKeyboardButton("Support Developer", callback_data="dashboard:support")],
        ]
    )


def verification_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for label, url in settings.public_channel_links():
        rows.append([InlineKeyboardButton(f"Join {label}", url=url)])
    rows.append([InlineKeyboardButton("I joined", callback_data="verify")])
    return InlineKeyboardMarkup(rows)


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="dashboard:home")]])
