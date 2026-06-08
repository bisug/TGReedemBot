from __future__ import annotations

import asyncio
from dataclasses import dataclass

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Message, MessageEntity, Update
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from src.config import Settings
from src.database import Database
from src.database.models import RedeemCode, User
from src.domain import RedeemCodeStatus, WithdrawalStatus
from src.helpers.common import get_database, get_settings
from src.helpers.security import enforce_rate_limit, require_private_chat
from src.services import ApprovalResult, RedeemService
from src.utils.limits import (
    MAX_BROADCAST_LENGTH,
    MAX_CLAIM_CODE_REDEMPTIONS,
    MAX_POINTS_PER_CLAIM_CODE,
    MAX_REDEEM_CODES_PER_BATCH,
    MAX_REJECTION_REASON_LENGTH,
    clamp_text,
    is_valid_claim_code,
)
from src.utils.ui import Emoji, ce, code_block, h, quote_block


@dataclass(frozen=True, slots=True)
class ApprovalDeliveryPlan:
    result: ApprovalResult
    user_telegram_id: int | None
    should_deliver: bool


@dataclass(frozen=True, slots=True)
class BroadcastPayload:
    text: str | None = None
    entities: tuple[MessageEntity, ...] = ()
    copy_from_chat_id: int | str | None = None
    copy_message_id: int | None = None


CODE_LIST_PAGE_MAX_CHARS = 3200


async def _require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not await require_private_chat(update):
        return False
    if not await enforce_rate_limit(update, context, "admin", limit=40, window_seconds=60):
        return False

    settings = get_settings(context)
    telegram_user = update.effective_user
    if settings.is_admin(telegram_user.id if telegram_user else None):
        return True
    if update.effective_message:
        await update.effective_message.reply_text(
            f"{ce('🚫', Emoji.PROHIBITED)} You are not allowed to use admin commands.",
            parse_mode=ParseMode.HTML,
        )
    return False


def _command_body(update: Update) -> str:
    text = update.effective_message.text if update.effective_message else ""
    parts = text.split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


def _plain_command_list(lines: list[str]) -> str:
    return "\n".join(f"- {h(line)}" for line in lines)


def _chunk_lines(lines: list[str], *, max_chars: int = 3400) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_chars = 0

    for line in lines:
        next_chars = len(line) + 1
        if current and current_chars + next_chars > max_chars:
            chunks.append("\n".join(current))
            current = []
            current_chars = 0
        current.append(line)
        current_chars += next_chars

    if current:
        chunks.append("\n".join(current))
    return chunks


def _code_status_filter(raw_status: str) -> tuple[str | None, str, str] | None:
    aliases = {
        "all": (None, "all", "all"),
        "available": (RedeemCodeStatus.AVAILABLE, "unused", "unused"),
        "unused": (RedeemCodeStatus.AVAILABLE, "unused", "unused"),
        "reserved": (RedeemCodeStatus.RESERVED, "reserved", "reserved"),
        "sent": (RedeemCodeStatus.SENT, "used", "used"),
        "used": (RedeemCodeStatus.SENT, "used", "used"),
    }
    return aliases.get(raw_status.lower())


def _code_status_icon(status: str) -> str:
    return "🟢" if status == RedeemCodeStatus.AVAILABLE else "🔴"


def _code_status_label(status: str) -> str:
    if status == RedeemCodeStatus.AVAILABLE:
        return "unused"
    if status == RedeemCodeStatus.RESERVED:
        return "reserved"
    if status == RedeemCodeStatus.SENT:
        return "used"
    return status


def _code_list_line(code: RedeemCode) -> str:
    return f"#{code.id} {_code_status_icon(code.status)} {h(_code_status_label(code.status))} <code>{h(code.code)}</code>"


def _code_list_keyboard(*, status_key: str, page: int, total_pages: int) -> InlineKeyboardMarkup | None:
    if total_pages <= 1:
        return None

    buttons: list[InlineKeyboardButton] = []
    if page > 0:
        buttons.append(InlineKeyboardButton("Back", callback_data=f"admin:codes:{status_key}:{page - 1}"))
    if page + 1 < total_pages:
        buttons.append(InlineKeyboardButton("Next", callback_data=f"admin:codes:{status_key}:{page + 1}"))
    return InlineKeyboardMarkup([buttons]) if buttons else None


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _broadcast_command_end(message: Message) -> int | None:
    text = message.text or ""
    question_command = "?broadcast"
    if text.startswith(question_command) and (
        len(text) == len(question_command) or text[len(question_command)].isspace()
    ):
        return len(question_command)

    for entity in message.entities or ():
        if entity.type != MessageEntity.BOT_COMMAND or entity.offset != 0:
            continue
        command_text = text[: entity.length]
        command_name = command_text.split("@", maxsplit=1)[0]
        if command_name == "/broadcast":
            return len(command_text)
    return None


def _body_start_index(text: str, command_end: int) -> int:
    body_start = command_end
    while body_start < len(text) and text[body_start].isspace():
        body_start += 1
    return body_start


def _slice_entities(
    text: str,
    entities: tuple[MessageEntity, ...] | list[MessageEntity] | None,
    start_index: int,
) -> tuple[MessageEntity, ...]:
    if not entities:
        return ()

    start_offset = _utf16_length(text[:start_index])
    end_offset = _utf16_length(text)
    kept = [
        entity
        for entity in entities
        if entity.offset >= start_offset and entity.offset + entity.length <= end_offset
    ]
    if not kept:
        return ()
    return tuple(MessageEntity.shift_entities(-start_offset, kept))


def _broadcast_payload_from_message(message: Message) -> BroadcastPayload | None:
    text = message.text or ""
    command_end = _broadcast_command_end(message)
    if command_end is not None:
        body_start = _body_start_index(text, command_end)
        body = text[body_start:]
        if body:
            return BroadcastPayload(
                text=body,
                entities=_slice_entities(text, message.entities, body_start),
            )

    if message.reply_to_message is not None:
        reply = message.reply_to_message
        return BroadcastPayload(copy_from_chat_id=reply.chat_id, copy_message_id=reply.message_id)
    return None


async def _send_broadcast_message(bot: Bot, telegram_id: int, payload: BroadcastPayload) -> bool:
    for attempt in range(2):
        try:
            if payload.copy_message_id is not None and payload.copy_from_chat_id is not None:
                await bot.copy_message(
                    chat_id=telegram_id,
                    from_chat_id=payload.copy_from_chat_id,
                    message_id=payload.copy_message_id,
                )
            else:
                await bot.send_message(
                    chat_id=telegram_id,
                    text=payload.text or "",
                    entities=payload.entities or None,
                )
            return True
        except RetryAfter as exc:
            if attempt:
                return False
            retry_after = exc.retry_after
            delay = retry_after.total_seconds() if hasattr(retry_after, "total_seconds") else float(retry_after)
            await asyncio.sleep(delay + 0.1)
        except TelegramError:
            return False
    return False


async def _broadcast_many(bot: Bot, user_ids: list[int], payload: BroadcastPayload, *, concurrency: int) -> tuple[int, int]:
    if not user_ids:
        return 0, 0

    sent = 0
    failed = 0
    next_index = 0

    async def worker() -> None:
        nonlocal sent, failed, next_index
        while next_index < len(user_ids):
            telegram_id = user_ids[next_index]
            next_index += 1
            if await _send_broadcast_message(bot, telegram_id, payload):
                sent += 1
            else:
                failed += 1

    worker_count = min(concurrency, len(user_ids))
    await asyncio.gather(*(worker() for _ in range(worker_count)))
    return sent, failed


async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    commands = _plain_command_list(
        [
            "/stats - Show users, points, stock, withdrawals, and Stars totals",
            "/broadcast <message> - Send a message to all registered users",
            "Reply /broadcast - Broadcast the replied message",
            "?broadcast <message> - Alternate broadcast prefix",
            "/genpoints <points> [max_uses] [custom_code] - Create a points claim code",
            "/addcodes - Add Google redeem code inventory, one code per line",
            "/codes [all|unused|reserved|used] - List redeem codes",
            "/updatecode <old_code> <new_code> - Replace an unused redeem code",
            "/removecode <code_or_id> - Remove an unused redeem code",
            "/stock - Show redeem code inventory counts",
            "/withdrawals - List open withdrawal records",
            "/approve <withdrawal_id> - Retry or manually approve a withdrawal",
            "/reject <withdrawal_id> [reason] - Reject without deducting points",
        ]
    )
    await update.effective_message.reply_text(
        f"{ce('⚙️', Emoji.GEAR)} <b>Admin Panel</b>\n\n"
        f"{quote_block('Use these commands to manage users, claim codes, redeem-code stock, and withdrawal requests.')}\n\n"
        f"{commands}",
        parse_mode=ParseMode.HTML,
    )


async def add_codes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    body = _command_body(update)
    if not body:
        example = code_block("/addcodes CODE-ONE\nCODE-TWO\nCODE-THREE")
        await update.effective_message.reply_text(
            f"{ce('➕', Emoji.PLUS)} <b>Add Redeem Codes</b>\n\n"
            "Please send the codes after /addcodes, one code per line.\n\n"
            f"<b>Example:</b>\n{example}",
            parse_mode=ParseMode.HTML,
        )
        return

    if "\n" in body:
        codes = [line.strip() for line in body.splitlines()]
    else:
        codes = [part.strip() for part in context.args]
    codes = codes[:MAX_REDEEM_CODES_PER_BATCH]

    settings = get_settings(context)
    db = get_database(context)
    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            added, skipped = await service.add_codes(codes)

    summary = f"Added: {added}\nSkipped duplicates: {skipped}"
    await update.effective_message.reply_text(
        f"{ce('✅', Emoji.SUCCESS)} <b>Inventory updated.</b>\n\n"
        f"{quote_block(summary)}",
        parse_mode=ParseMode.HTML,
    )


async def _show_codes_page(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    raw_status: str,
    page: int = 0,
    edit: bool = False,
) -> None:
    status_filter = _code_status_filter(raw_status)
    if status_filter is None:
        text = (
            f"{ce('🗄', Emoji.FILE_CABINET)} <b>Redeem Codes</b>\n\n"
            "Please choose one of: all, unused, reserved, used.\n\n"
            f"<b>Example:</b>\n{code_block('/codes unused')}"
        )
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML)
        elif update.effective_message:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)
        return

    status, label, status_key = status_filter
    settings = get_settings(context)
    db = get_database(context)
    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            rows = await service.list_redeem_codes(status=status)

    if not rows:
        text = (
            f"{ce('🗄', Emoji.FILE_CABINET)} <b>Redeem Codes</b>\n\n"
            f"{quote_block(f'No {label} redeem codes found.')}"
        )
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML)
        elif update.effective_message:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)
        return

    lines = [_code_list_line(code) for code in rows]
    pages = _chunk_lines(lines, max_chars=CODE_LIST_PAGE_MAX_CHARS)
    page = max(0, min(page, len(pages) - 1))
    summary = f"{len(rows)} code(s). Page {page + 1}/{len(pages)}."
    text = (
        f"{ce('🗄', Emoji.FILE_CABINET)} <b>Redeem Codes - {h(label.title())}</b>\n"
        f"{quote_block(summary)}\n\n"
        f"{pages[page]}"
    )
    reply_markup = _code_list_keyboard(status_key=status_key, page=page, total_pages=len(pages))

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
        )
    elif update.effective_message:
        await update.effective_message.reply_text(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
        )


async def codes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    raw_status = context.args[0].lower() if context.args else "all"
    await _show_codes_page(update, context, raw_status=raw_status)


async def codes_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    query = update.callback_query
    if query is None or not query.data:
        return
    await query.answer()
    _prefix, _section, raw_status, raw_page = query.data.split(":", maxsplit=3)
    page = int(raw_page) if raw_page.isdigit() else 0
    await _show_codes_page(update, context, raw_status=raw_status, page=page, edit=True)


async def update_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    if len(context.args) != 2:
        await update.effective_message.reply_text(
            f"{ce('📝', Emoji.MEMO)} <b>Update Redeem Code</b>\n\n"
            "Please provide the current unused code and the replacement code.\n\n"
            f"<b>Example:</b>\n{code_block('/updatecode OLD-CODE NEW-CODE')}",
            parse_mode=ParseMode.HTML,
        )
        return

    settings = get_settings(context)
    db = get_database(context)
    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            success, message = await service.update_available_code(context.args[0], context.args[1])

    icon = ce('✅', Emoji.SUCCESS) if success else ce('❌', Emoji.CROSS)
    title = "Redeem code updated." if success else "Could not update redeem code."
    await update.effective_message.reply_text(
        f"{icon} <b>{title}</b>\n\n{quote_block(message)}",
        parse_mode=ParseMode.HTML,
    )


async def remove_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    if len(context.args) != 1:
        await update.effective_message.reply_text(
            f"{ce('🗑', Emoji.WASTEBASKET)} <b>Remove Redeem Code</b>\n\n"
            "Please provide one unused redeem code or the ID shown in /codes.\n\n"
            f"<b>Example:</b>\n{code_block('/removecode #42')}",
            parse_mode=ParseMode.HTML,
        )
        return

    settings = get_settings(context)
    db = get_database(context)
    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            success, message = await service.remove_available_code(context.args[0])

    icon = ce('✅', Emoji.SUCCESS) if success else ce('❌', Emoji.CROSS)
    title = "Redeem code removed." if success else "Could not remove redeem code."
    await update.effective_message.reply_text(
        f"{icon} <b>{title}</b>\n\n{quote_block(message)}",
        parse_mode=ParseMode.HTML,
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    settings = get_settings(context)
    db = get_database(context)
    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            values = await service.admin_stats()

    stats_text = (
        f"Users: {values['total_users']}\n"
        f"Verified users: {values['verified_users']}\n"
        f"Pending withdrawals: {values['pending_withdrawals']}\n"
        f"Unused redeem codes: {values['available_codes']}\n"
        f"Used redeem codes: {values['sent_codes']}\n"
        f"Total active points: {values['total_points']}\n"
        f"Paid Stars: {values['paid_stars']}\n"
        f"Active claim codes: {values['active_claim_codes']}"
    )
    await update.effective_message.reply_text(
        f"{ce('📊', Emoji.BAR_CHART)} <b>Bot Statistics</b>\n\n"
        f"{quote_block(stats_text)}",
        parse_mode=ParseMode.HTML,
    )


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    message = update.effective_message
    if message is None:
        return
    payload = _broadcast_payload_from_message(message)
    if payload is None:
        await update.effective_message.reply_text(
            f"{ce('🔔', Emoji.BELL)} <b>Broadcast</b>\n\n"
            "Please include the message you want to broadcast or reply to a message with /broadcast.\n\n"
            f"<b>Examples:</b>\n{code_block('/broadcast New claim code is live.\n?broadcast New claim code is live.')}",
            parse_mode=ParseMode.HTML,
        )
        return
    if payload.text is not None and len(payload.text) > MAX_BROADCAST_LENGTH:
        await update.effective_message.reply_text(
            f"{ce('⚠️', Emoji.WARNING)} <b>Broadcast too long.</b>\n\n"
            f"{quote_block(f'Keep broadcasts under {MAX_BROADCAST_LENGTH} characters.')}",
            parse_mode=ParseMode.HTML,
        )
        return

    settings = get_settings(context)
    db = get_database(context)
    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            user_ids = await service.all_user_telegram_ids()

    sent, failed = await _broadcast_many(
        context.bot,
        user_ids,
        payload,
        concurrency=settings.broadcast_concurrency,
    )

    result_text = f"Sent: {sent}\nFailed: {failed}"
    await update.effective_message.reply_text(
        f"{ce('✅', Emoji.SUCCESS)} <b>Broadcast finished.</b>\n\n"
        f"{quote_block(result_text)}",
        parse_mode=ParseMode.HTML,
    )


async def genpoints(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        examples = code_block("/genpoints 5\n/genpoints 10 50 BONUS10")
        await update.effective_message.reply_text(
            f"{ce('🪙', Emoji.COIN)} <b>Generate Points Code</b>\n\n"
            "Please provide how many points the claim code should give.\n\n"
            f"<b>Examples:</b>\n{examples}",
            parse_mode=ParseMode.HTML,
        )
        return

    points = int(context.args[0])
    if points > MAX_POINTS_PER_CLAIM_CODE:
        await update.effective_message.reply_text(
            f"{ce('⚠️', Emoji.WARNING)} <b>Invalid points amount.</b>\n\n"
            f"{quote_block(f'Use {MAX_POINTS_PER_CLAIM_CODE} points or less per claim code.')}",
            parse_mode=ParseMode.HTML,
        )
        return
    max_redemptions = 1
    custom_code: str | None = None
    if len(context.args) >= 2:
        if context.args[1].isdigit():
            max_redemptions = int(context.args[1])
            custom_code = context.args[2] if len(context.args) >= 3 else None
        else:
            custom_code = context.args[1]
    if max_redemptions > MAX_CLAIM_CODE_REDEMPTIONS:
        await update.effective_message.reply_text(
            f"{ce('⚠️', Emoji.WARNING)} <b>Invalid max uses.</b>\n\n"
            f"{quote_block(f'Use {MAX_CLAIM_CODE_REDEMPTIONS} uses or fewer per claim code.')}",
            parse_mode=ParseMode.HTML,
        )
        return
    if custom_code and not is_valid_claim_code(custom_code):
        await update.effective_message.reply_text(
            f"{ce('⚠️', Emoji.WARNING)} <b>Invalid custom code.</b>\n\n"
            f"{quote_block('Use only letters, numbers, underscores, and dashes.')}",
            parse_mode=ParseMode.HTML,
        )
        return

    settings = get_settings(context)
    db = get_database(context)
    try:
        async with db.session() as session:
            async with session.begin():
                service = RedeemService(session, settings)
                claim_code = await service.generate_claim_code(
                    points=points,
                    max_redemptions=max_redemptions,
                    admin_telegram_id=update.effective_user.id,
                    code=custom_code,
                )
    except ValueError as exc:
        await update.effective_message.reply_text(
            f"{ce('❌', Emoji.CROSS)} <b>Could not create claim code.</b>\n\n{quote_block(exc)}",
            parse_mode=ParseMode.HTML,
        )
        return

    redeem_hint = f"Users can redeem it with /claim {claim_code.code}"
    await update.effective_message.reply_text(
        f"{ce('✅', Emoji.SUCCESS)} <b>Points claim code created.</b>\n\n"
        f"Code: <code>{h(claim_code.code)}</code>\n"
        f"Points: {claim_code.points}\n"
        f"Max uses: {claim_code.max_redemptions}\n\n"
        f"{quote_block(redeem_hint)}",
        parse_mode=ParseMode.HTML,
    )


async def stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    settings = get_settings(context)
    db = get_database(context)
    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            counts = await service.stock_counts()

    stock_text = (
        f"Unused: {counts.get('available', 0)}\n"
        f"Reserved: {counts.get('reserved', 0)}\n"
        f"Used: {counts.get('sent', 0)}"
    )
    await update.effective_message.reply_text(
        f"{ce('🗄', Emoji.FILE_CABINET)} <b>Redeem Code Stock</b>\n\n"
        f"{quote_block(stock_text)}",
        parse_mode=ParseMode.HTML,
    )


async def withdrawals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    settings = get_settings(context)
    db = get_database(context)
    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            rows = await service.pending_withdrawals()

    if not rows:
        await update.effective_message.reply_text(
            f"{ce('⏰', Emoji.ALARM)} <b>Open Withdrawals</b>\n\n"
            f"{quote_block('There are no open withdrawal records.')}",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = []
    for withdrawal, user in rows:
        label = f"@{user.username}" if user.username else str(user.telegram_id)
        status = f" - {h(withdrawal.status)}" if withdrawal.status != WithdrawalStatus.PENDING else ""
        lines.append(f"#{withdrawal.id} - {h(label)} - {withdrawal.points_cost} points{status}")
    withdrawal_text = "\n".join(lines)
    await update.effective_message.reply_text(
        f"{ce('⏰', Emoji.ALARM)} <b>Open Withdrawals</b>\n\n{quote_block(withdrawal_text)}",
        parse_mode=ParseMode.HTML,
    )


async def _reserve_approval_delivery(
    *,
    db: Database,
    settings: Settings,
    withdrawal_id: int,
    admin_telegram_id: int,
) -> ApprovalDeliveryPlan:
    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            result = await service.reserve_withdrawal_approval(
                withdrawal_id,
                admin_telegram_id=admin_telegram_id,
            )
            user_telegram_id: int | None = None
            should_deliver = False
            if result.withdrawal is not None:
                user = await session.get(User, result.withdrawal.user_id)
                user_telegram_id = user.telegram_id if user else None
                should_deliver = result.withdrawal.status == WithdrawalStatus.RESERVED
            return ApprovalDeliveryPlan(result, user_telegram_id, should_deliver)


async def _send_reserved_code(context: ContextTypes.DEFAULT_TYPE, *, telegram_id: int, code: str) -> None:
    await context.bot.send_message(
        chat_id=telegram_id,
        text=(
            f"{ce('✅', Emoji.SUCCESS)} <b>Your withdrawal was approved.</b>\n\n"
            f"<b>Google redeem code:</b>\n{code_block(code)}\n"
            f"{quote_block('Keep this code private and redeem it from your Google account.')}"
        ),
        parse_mode=ParseMode.HTML,
    )


async def _finalize_approval_delivery(
    *,
    db: Database,
    settings: Settings,
    withdrawal_id: int,
    admin_telegram_id: int,
) -> ApprovalResult:
    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            return await service.finalize_reserved_withdrawal(
                withdrawal_id,
                admin_telegram_id=admin_telegram_id,
            )


async def _mark_approval_delivery_failed(
    *,
    db: Database,
    settings: Settings,
    withdrawal_id: int,
    admin_telegram_id: int,
    error: TelegramError,
) -> ApprovalResult:
    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            return await service.mark_withdrawal_delivery_failed(
                withdrawal_id,
                admin_telegram_id=admin_telegram_id,
                reason=str(error),
            )


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text(
            f"{ce('✅', Emoji.SUCCESS)} <b>Approve Withdrawal</b>\n\n"
            "Please provide a withdrawal ID.\n\n"
            f"<b>Example:</b>\n{code_block('/approve 12')}",
            parse_mode=ParseMode.HTML,
        )
        return

    withdrawal_id = int(context.args[0])
    settings = get_settings(context)
    db = get_database(context)
    admin_telegram_id = update.effective_user.id
    delivery = await _reserve_approval_delivery(
        db=db,
        settings=settings,
        withdrawal_id=withdrawal_id,
        admin_telegram_id=admin_telegram_id,
    )
    result = delivery.result

    if result.success and result.code and delivery.user_telegram_id is not None and delivery.should_deliver:
        try:
            await _send_reserved_code(context, telegram_id=delivery.user_telegram_id, code=result.code)
        except TelegramError as exc:
            result = await _mark_approval_delivery_failed(
                db=db,
                settings=settings,
                withdrawal_id=withdrawal_id,
                admin_telegram_id=admin_telegram_id,
                error=exc,
            )
        else:
            result = await _finalize_approval_delivery(
                db=db,
                settings=settings,
                withdrawal_id=withdrawal_id,
                admin_telegram_id=admin_telegram_id,
            )
    await update.effective_message.reply_text(
        f"{ce('✅', Emoji.SUCCESS)} <b>Approval Result</b>\n\n{quote_block(result.message)}",
        parse_mode=ParseMode.HTML,
    )


async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text(
            f"{ce('❌', Emoji.CROSS)} <b>Reject Withdrawal</b>\n\n"
            "Please provide a withdrawal ID.\n\n"
            f"<b>Example:</b>\n{code_block('/reject 12 Not enough valid activity')}",
            parse_mode=ParseMode.HTML,
        )
        return

    withdrawal_id = int(context.args[0])
    reason = clamp_text(" ".join(context.args[1:]), MAX_REJECTION_REASON_LENGTH) or None
    settings = get_settings(context)
    db = get_database(context)
    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            result = await service.reject_withdrawal(
                withdrawal_id, admin_telegram_id=update.effective_user.id, reason=reason
            )
            user_telegram_id: int | None = None
            if result.withdrawal is not None:
                user = await session.get(User, result.withdrawal.user_id)
                user_telegram_id = user.telegram_id if user else None

    if result.success and user_telegram_id is not None:
        message = "Your withdrawal request was rejected. Your points were not deducted."
        if reason:
            message += f"\nReason: {reason}"
        await context.bot.send_message(
            chat_id=user_telegram_id,
            text=f"{ce('❌', Emoji.CROSS)} <b>Withdrawal Rejected</b>\n\n{quote_block(message)}",
            parse_mode=ParseMode.HTML,
        )
    await update.effective_message.reply_text(
        f"{ce('❌', Emoji.CROSS)} <b>Rejection Result</b>\n\n{quote_block(result.message)}",
        parse_mode=ParseMode.HTML,
    )


def register_admin_handlers(application: Application) -> None:
    application.add_handler(CommandHandler("admin", admin_menu))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^\?broadcast(?:\s|$)"), broadcast))
    application.add_handler(CommandHandler("genpoints", genpoints))
    application.add_handler(CommandHandler("addcodes", add_codes))
    application.add_handler(CommandHandler("codes", codes))
    application.add_handler(CallbackQueryHandler(codes_page, pattern=r"^admin:codes:(?:all|unused|reserved|used):\d+$"))
    application.add_handler(CommandHandler("updatecode", update_code))
    application.add_handler(CommandHandler("removecode", remove_code))
    application.add_handler(CommandHandler("stock", stock))
    application.add_handler(CommandHandler("withdrawals", withdrawals))
    application.add_handler(CommandHandler("approve", approve))
    application.add_handler(CommandHandler("reject", reject))
