from __future__ import annotations

import asyncio

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import CommandHandler, ContextTypes

from src.database.models import User
from src.helpers.common import get_database, get_settings
from src.services import RedeemService
from src.utils.ui import Emoji, ce, code_block, h, quote_block


async def _require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    settings = get_settings(context)
    telegram_user = update.effective_user
    if settings.is_admin(telegram_user.id if telegram_user else None):
        return True
    if update.effective_message:
        await update.effective_message.reply_text(
            f"{ce('🚪', Emoji.EXIT)} You are not allowed to use admin commands.",
            parse_mode=ParseMode.HTML,
        )
    return False


def _command_body(update: Update) -> str:
    text = update.effective_message.text if update.effective_message else ""
    parts = text.split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    commands = code_block(
        "/stats - Show users, points, stock, withdrawals, and Stars totals\n"
        "/broadcast <message> - Send a message to all registered users\n"
        "/genpoints <points> [max_uses] [custom_code] - Create a points claim code\n"
        "/addcodes - Add Google redeem code inventory, one code per line\n"
        "/stock - Show redeem code inventory counts\n"
        "/withdrawals - List pending withdrawal requests\n"
        "/approve <withdrawal_id> - Approve a request and send a code\n"
        "/reject <withdrawal_id> [reason] - Reject a request without deducting points"
    )
    await update.effective_message.reply_text(
        f"{ce('📂', Emoji.FOLDER)} <b>Admin Panel</b>\n\n"
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
            f"{ce('📂', Emoji.FOLDER)} <b>Add Redeem Codes</b>\n\n"
            "Please send the codes after /addcodes, one code per line.\n\n"
            f"<b>Example:</b>\n{example}",
            parse_mode=ParseMode.HTML,
        )
        return

    if "\n" in body:
        codes = [line.strip() for line in body.splitlines()]
    else:
        codes = [part.strip() for part in context.args]

    settings = get_settings(context)
    db = get_database(context)
    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            added, skipped = await service.add_codes(codes)

    summary = f"Added: {added}\nSkipped duplicates: {skipped}"
    await update.effective_message.reply_text(
        f"{ce('✔️', Emoji.CHECK)} <b>Inventory updated.</b>\n\n"
        f"{quote_block(summary)}",
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
        f"Available redeem codes: {values['available_codes']}\n"
        f"Sent redeem codes: {values['sent_codes']}\n"
        f"Total active points: {values['total_points']}\n"
        f"Paid Stars: {values['paid_stars']}\n"
        f"Active claim codes: {values['active_claim_codes']}"
    )
    await update.effective_message.reply_text(
        f"{ce('🔲', Emoji.MENU)} <b>Bot Statistics</b>\n\n"
        f"{quote_block(stats_text)}",
        parse_mode=ParseMode.HTML,
    )


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    body = _command_body(update)
    if not body:
        await update.effective_message.reply_text(
            f"{ce('📲', Emoji.PHONE_ALT)} <b>Broadcast</b>\n\n"
            "Please include the message you want to broadcast.\n\n"
            f"<b>Example:</b>\n{code_block('/broadcast New claim code is live.')}",
            parse_mode=ParseMode.HTML,
        )
        return

    settings = get_settings(context)
    db = get_database(context)
    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            user_ids = await service.all_user_telegram_ids()

    sent = 0
    failed = 0
    for telegram_id in user_ids:
        try:
            await context.bot.send_message(chat_id=telegram_id, text=body)
            sent += 1
            await asyncio.sleep(0.05)
        except TelegramError:
            failed += 1

    result_text = f"Sent: {sent}\nFailed: {failed}"
    await update.effective_message.reply_text(
        f"{ce('✔️', Emoji.CHECK)} <b>Broadcast finished.</b>\n\n"
        f"{quote_block(result_text)}",
        parse_mode=ParseMode.HTML,
    )


async def genpoints(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        examples = code_block("/genpoints 5\n/genpoints 10 50 BONUS10")
        await update.effective_message.reply_text(
            f"{ce('📂', Emoji.FOLDER_ALT)} <b>Generate Points Code</b>\n\n"
            "Please provide how many points the claim code should give.\n\n"
            f"<b>Examples:</b>\n{examples}",
            parse_mode=ParseMode.HTML,
        )
        return

    points = int(context.args[0])
    max_redemptions = 1
    custom_code: str | None = None
    if len(context.args) >= 2:
        if context.args[1].isdigit():
            max_redemptions = int(context.args[1])
            custom_code = context.args[2] if len(context.args) >= 3 else None
        else:
            custom_code = context.args[1]

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
            f"{ce('🚪', Emoji.EXIT)} <b>Could not create claim code.</b>\n\n{quote_block(exc)}",
            parse_mode=ParseMode.HTML,
        )
        return

    redeem_hint = f"Users can redeem it with /claim {claim_code.code}"
    await update.effective_message.reply_text(
        f"{ce('✔️', Emoji.CHECK)} <b>Points claim code created.</b>\n\n"
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
        f"Available: {counts.get('available', 0)}\n"
        f"Reserved: {counts.get('reserved', 0)}\n"
        f"Sent: {counts.get('sent', 0)}"
    )
    await update.effective_message.reply_text(
        f"{ce('📂', Emoji.FOLDER)} <b>Redeem Code Stock</b>\n\n"
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
            f"{ce('✔️', Emoji.CHECK)} <b>Pending Withdrawals</b>\n\n"
            f"{quote_block('There are no pending withdrawal requests.')}",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = []
    for withdrawal, user in rows:
        label = f"@{user.username}" if user.username else str(user.telegram_id)
        lines.append(f"#{withdrawal.id} - {h(label)} - {withdrawal.points_cost} points")
    withdrawal_text = "\n".join(lines)
    await update.effective_message.reply_text(
        f"{ce('⬇️', Emoji.DOWNLOAD)} <b>Pending Withdrawals</b>\n\n{quote_block(withdrawal_text)}",
        parse_mode=ParseMode.HTML,
    )


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text(
            f"{ce('✔️', Emoji.CHECK)} <b>Approve Withdrawal</b>\n\n"
            "Please provide a withdrawal ID.\n\n"
            f"<b>Example:</b>\n{code_block('/approve 12')}",
            parse_mode=ParseMode.HTML,
        )
        return

    withdrawal_id = int(context.args[0])
    settings = get_settings(context)
    db = get_database(context)
    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            result = await service.approve_withdrawal(withdrawal_id, admin_telegram_id=update.effective_user.id)
            user_telegram_id: int | None = None
            if result.withdrawal is not None:
                user = await session.get(User, result.withdrawal.user_id)
                user_telegram_id = user.telegram_id if user else None

    if result.success and result.code and user_telegram_id is not None:
        await context.bot.send_message(
            chat_id=user_telegram_id,
            text=(
                f"{ce('✔️', Emoji.CHECK)} <b>Your withdrawal was approved.</b>\n\n"
                f"<b>Google redeem code:</b>\n{code_block(result.code)}\n"
                f"{quote_block('Keep this code private and redeem it from your Google account.')}"
            ),
            parse_mode=ParseMode.HTML,
        )
    await update.effective_message.reply_text(
        f"{ce('✔️', Emoji.CHECK)} <b>Approval Result</b>\n\n{quote_block(result.message)}",
        parse_mode=ParseMode.HTML,
    )


async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text(
            f"{ce('🚪', Emoji.EXIT)} <b>Reject Withdrawal</b>\n\n"
            "Please provide a withdrawal ID.\n\n"
            f"<b>Example:</b>\n{code_block('/reject 12 Not enough valid activity')}",
            parse_mode=ParseMode.HTML,
        )
        return

    withdrawal_id = int(context.args[0])
    reason = " ".join(context.args[1:]).strip() or None
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
            text=f"{ce('🚪', Emoji.EXIT)} <b>Withdrawal Rejected</b>\n\n{quote_block(message)}",
            parse_mode=ParseMode.HTML,
        )
    await update.effective_message.reply_text(
        f"{ce('🚪', Emoji.EXIT)} <b>Rejection Result</b>\n\n{quote_block(result.message)}",
        parse_mode=ParseMode.HTML,
    )


def register_admin_handlers(application) -> None:  # type: ignore[no-untyped-def]
    application.add_handler(CommandHandler("admin", admin_menu))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CommandHandler("genpoints", genpoints))
    application.add_handler(CommandHandler("addcodes", add_codes))
    application.add_handler(CommandHandler("stock", stock))
    application.add_handler(CommandHandler("withdrawals", withdrawals))
    application.add_handler(CommandHandler("approve", approve))
    application.add_handler(CommandHandler("reject", reject))
