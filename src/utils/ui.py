from __future__ import annotations

from html import escape

from telegram import InlineKeyboardButton


class Emoji:
    MENU = "5445385947069838800"
    PHONE = "5445059250382469069"
    PHONE_ALT = "5445033158456145975"
    GLOBE = "5447602197439218445"
    BACK = "5447506720316225765"
    NEXT = "5445350109862720603"
    BACK_ALT = "5447389832781264371"
    NEXT_ALT = "5447181973544008180"
    FOLDER = "5447282724886839705"
    EXIT = "5447434637880098257"
    DOWNLOAD = "5444961234933806330"
    FOLDER_ALT = "5447389837076231920"
    CHECK = "5447242579827523388"
    CHECK_ALT = "5444987348334965906"
    CHECK_MARK = "5445210909972655435"


def h(value: object) -> str:
    return escape(str(value), quote=False)


def quote_block(value: object) -> str:
    return f"<blockquote>{h(value)}</blockquote>"


def code_block(value: object) -> str:
    return f"<pre>{h(value)}</pre>"


def ce(fallback: str, emoji_id: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{h(fallback)}</tg-emoji>'


def styled_button(
    text: str,
    *,
    emoji_id: str | None = None,
    style: str | None = None,
    callback_data: str | None = None,
    url: str | None = None,
) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text,
        callback_data=callback_data,
        url=url,
        icon_custom_emoji_id=emoji_id,
        style=style,
    )
