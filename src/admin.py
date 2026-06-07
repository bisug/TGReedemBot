from __future__ import annotations

import asyncio

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import CommandHandler, ContextTypes

from src.common import get_database, get_settings
from src.models import User
from src.service import RedeemService


async def _require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    settings = get_settings(context)
    telegram_user = update.effective_user
    if settings.is_admin(telegram_user.id if telegram_user else None):
        return True
    if update.effective_message:
        await update.effective_message.reply_text("You are not allowed to use admin commands.")
    return False


def _command_body(update: Update) -> str:
    text = update.effective_message.text if update.effective_message else ""
    parts = text.split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    await update.effective_message.reply_text(
        "Admin Panel\n\n"
        "/stats - Show users, points, stock, withdrawals, and Stars totals\n"
        "/broadcast <message> - Send a message to all registered users\n"
        "/genpoints <points> [max_uses] [custom_code] - Create a points claim code\n"
        "/addcodes - Add Google redeem code inventory, one code per line\n"
        "/stock - Show redeem code inventory counts\n"
        "/withdrawals - List pending withdrawal requests\n"
        "/approve <withdrawal_id> - Approve a request and send a code\n"
        "/reject <withdrawal_id> [reason] - Reject a request without deducting points"
    )


async def add_codes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    body = _command_body(update)
    if not body:
        await update.effective_message.reply_text(
            "Please send the codes after /addcodes, one code per line.\n\n"
            "Example:\n/addcodes CODE-ONE\nCODE-TWO\nCODE-THREE"
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

    await update.effective_message.reply_text(
        f"Inventory updated.\n\nAdded: {added}\nSkipped duplicates: {skipped}"
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

    await update.effective_message.reply_text(
        "Bot Statistics\n\n"
        f"Users: {values['total_users']}\n"
        f"Verified users: {values['verified_users']}\n"
        f"Pending withdrawals: {values['pending_withdrawals']}\n"
        f"Available redeem codes: {values['available_codes']}\n"
        f"Sent redeem codes: {values['sent_codes']}\n"
        f"Total active points: {values['total_points']}\n"
        f"Paid Stars: {values['paid_stars']}\n"
        f"Active claim codes: {values['active_claim_codes']}"
    )


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    body = _command_body(update)
    if not body:
        await update.effective_message.reply_text(
            "Please include the message you want to broadcast.\n\nExample:\n/broadcast New claim code is live."
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

    await update.effective_message.reply_text(
        f"Broadcast finished.\n\nSent: {sent}\nFailed: {failed}"
    )


async def genpoints(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text(
            "Please provide how many points the claim code should give.\n\n"
            "Examples:\n"
            "/genpoints 5\n"
            "/genpoints 10 50 BONUS10"
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
        await update.effective_message.reply_text(str(exc))
        return

    await update.effective_message.reply_text(
        "Points claim code created.\n\n"
        f"Code: {claim_code.code}\n"
        f"Points: {claim_code.points}\n"
        f"Max uses: {claim_code.max_redemptions}\n\n"
        f"Users can redeem it with /claim {claim_code.code}"
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

    await update.effective_message.reply_text(
        "Redeem Code Stock\n\n"
        f"Available: {counts.get('available', 0)}\n"
        f"Reserved: {counts.get('reserved', 0)}\n"
        f"Sent: {counts.get('sent', 0)}"
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
        await update.effective_message.reply_text("There are no pending withdrawal requests.")
        return

    lines = ["Pending Withdrawals"]
    for withdrawal, user in rows:
        label = f"@{user.username}" if user.username else str(user.telegram_id)
        lines.append(f"#{withdrawal.id} - {label} - {withdrawal.points_cost} points")
    await update.effective_message.reply_text("\n".join(lines))


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Please provide a withdrawal ID.\n\nExample:\n/approve 12")
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
                "Your withdrawal was approved.\n\n"
                f"Google redeem code:\n{result.code}\n\n"
                "Keep this code private and redeem it from your Google account."
            ),
        )
    await update.effective_message.reply_text(result.message)


async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text(
            "Please provide a withdrawal ID.\n\nExample:\n/reject 12 Not enough valid activity"
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
        await context.bot.send_message(chat_id=user_telegram_id, text=message)
    await update.effective_message.reply_text(result.message)


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
