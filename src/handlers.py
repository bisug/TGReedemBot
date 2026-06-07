from __future__ import annotations

from telegram import LabeledPrice, Update
from telegram.error import BadRequest
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from src.common import (
    answer_callback,
    get_database,
    get_settings,
    has_required_channels,
    parse_referral_arg,
)
from src.keyboards import back_keyboard, dashboard_keyboard, verification_keyboard
from src.service import RedeemService
from src.ui import Emoji, ce, h


async def _send_or_edit(update: Update, text: str, **kwargs) -> None:
    if update.callback_query and update.callback_query.message:
        try:
            await update.callback_query.edit_message_text(text, **kwargs)
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
    elif update.effective_message:
        await update.effective_message.reply_text(text, **kwargs)


async def _show_verification(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    if settings.required_channel_ids:
        channel_lines = "\n".join(f"- {channel}" for channel in settings.required_channel_ids)
        text = (
            f"{ce('✔️', Emoji.CHECK)} <b>Verification Required</b>\n\n"
            "Before you can use the dashboard, please join the required channel(s).\n\n"
            f"<b>Required channel(s):</b>\n{h(channel_lines)}\n\n"
            "After joining, press I joined and I will verify your membership."
        )
    else:
        text = f"{ce('✔️', Emoji.CHECK)} <b>Verification</b>\n\nPress I joined to continue to your dashboard."
    await _send_or_edit(update, text, reply_markup=verification_keyboard(settings), parse_mode=ParseMode.HTML)


async def _show_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = get_database(context)
    settings = get_settings(context)
    telegram_user = update.effective_user
    if telegram_user is None:
        return
    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            user = await service.get_user_by_telegram_id(telegram_user.id)
            if user is None:
                user = await service.get_or_create_user(
                    telegram_id=telegram_user.id,
                    username=telegram_user.username,
                    first_name=telegram_user.first_name,
                )
            points = user.point_balance
    await _send_or_edit(
        update,
        (
            f"{ce('🔲', Emoji.MENU)} <b>Redeem Code Dashboard</b>\n\n"
            f"<b>Available points:</b> {points}\n"
            f"<b>Withdrawal requirement:</b> {settings.withdraw_cost_points} points\n\n"
            "Earn points from referrals and claim codes. When you have enough points, request a Google redeem code."
        ),
        reply_markup=dashboard_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    db = get_database(context)
    telegram_user = update.effective_user
    if telegram_user is None:
        return

    referral_telegram_id = parse_referral_arg(context.args)
    channel_ok = await has_required_channels(context.bot, settings, telegram_user.id)

    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            await service.get_or_create_user(
                telegram_id=telegram_user.id,
                username=telegram_user.username,
                first_name=telegram_user.first_name,
                referral_telegram_id=referral_telegram_id,
            )
            if channel_ok:
                await service.mark_verified_and_award(telegram_user.id)

    if not channel_ok:
        await _show_verification(update, context)
        return
    if update.effective_message:
        await update.effective_message.reply_text(
            f"{ce('🔲', Emoji.MENU)} <b>Welcome to Redeem Code Bot</b>\n\n"
            "Use the dashboard below to check your points, invite friends, request withdrawals, or support the developer.",
            parse_mode=ParseMode.HTML,
        )
    await _show_dashboard(update, context)


async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer_callback(update)
    settings = get_settings(context)
    db = get_database(context)
    telegram_user = update.effective_user
    if telegram_user is None:
        return

    if not await has_required_channels(context.bot, settings, telegram_user.id):
        await _show_verification(update, context)
        return

    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            await service.mark_verified_and_award(telegram_user.id)
    await _show_dashboard(update, context)


async def dashboard_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer_callback(update)
    await _show_dashboard(update, context)


async def referral_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer_callback(update)
    settings = get_settings(context)
    db = get_database(context)
    telegram_user = update.effective_user
    if telegram_user is None:
        return

    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            user = await service.get_user_by_telegram_id(telegram_user.id)
            if user is None:
                await _show_verification(update, context)
                return
            referral_points, successful_referrals = await service.referral_stats(user.id)

    link = f"https://t.me/{settings.bot_username}?start=ref_{telegram_user.id}"
    await _send_or_edit(
        update,
        (
            f"{ce('🌐', Emoji.GLOBE)} <b>Referral Program</b>\n\n"
            f"<b>Referral points:</b> {referral_points}\n"
            f"<b>Successful referrals:</b> {successful_referrals}\n\n"
            "Share this link with friends. You earn points after they start the bot and pass channel verification.\n\n"
            f"<b>Your referral link:</b>\n{h(link)}"
        ),
        reply_markup=back_keyboard(),
        disable_web_page_preview=True,
        parse_mode=ParseMode.HTML,
    )


async def withdraw_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer_callback(update)
    settings = get_settings(context)
    db = get_database(context)
    telegram_user = update.effective_user
    if telegram_user is None:
        return

    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            user = await service.get_user_by_telegram_id(telegram_user.id)
            if user is None:
                await _show_verification(update, context)
                return
            result = await service.create_withdrawal_request(telegram_user.id)
            latest = result.withdrawal or await service.latest_withdrawal_for_user(user.id)
            points = user.point_balance

    status = latest.status if latest is not None else "none"
    await _send_or_edit(
        update,
        (
            f"{ce('⬇️', Emoji.DOWNLOAD)} <b>Withdraw Google Redeem Code</b>\n\n"
            f"<b>Available points:</b> {points}\n"
            f"<b>Required points:</b> {settings.withdraw_cost_points}\n"
            f"<b>Withdrawal status:</b> {h(status)}\n\n"
            f"{h(result.message)}"
        ),
        reply_markup=back_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def support_developer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer_callback(update)
    settings = get_settings(context)
    db = get_database(context)
    telegram_user = update.effective_user
    if telegram_user is None:
        return

    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            payment = await service.create_star_payment(telegram_user.id, amount=settings.support_stars_amount)

    await context.bot.send_invoice(
        chat_id=telegram_user.id,
        title="Support Developer",
        description="Send a voluntary Telegram Stars donation to support development and hosting.",
        payload=payment.invoice_payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice("Support Developer", settings.support_stars_amount)],
        start_parameter=f"support_{telegram_user.id}",
    )


async def claim_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user = update.effective_user
    if telegram_user is None or update.effective_message is None:
        return
    if not context.args:
        await update.effective_message.reply_text(
            f"{ce('📂', Emoji.FOLDER)} <b>Claim Points</b>\n\n"
            "Please send the code you want to claim.\n\n"
            "Example:\n<code>/claim BONUS10</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    settings = get_settings(context)
    db = get_database(context)
    claim_code = context.args[0]
    channel_ok = await has_required_channels(context.bot, settings, telegram_user.id)

    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            await service.get_or_create_user(
                telegram_id=telegram_user.id,
                username=telegram_user.username,
                first_name=telegram_user.first_name,
            )
            if channel_ok:
                await service.mark_verified_and_award(telegram_user.id)
                result = await service.claim_points(
                    telegram_id=telegram_user.id,
                    username=telegram_user.username,
                    first_name=telegram_user.first_name,
                    code=claim_code,
                )
            else:
                result = None

    if not channel_ok:
        await _show_verification(update, context)
        return
    result_message = result.message if result else "Claim failed. Please try again."
    await update.effective_message.reply_text(
        f"{ce('✔️', Emoji.CHECK)} {h(result_message)}",
        parse_mode=ParseMode.HTML,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    settings = get_settings(context)
    telegram_user = update.effective_user
    lines = [
        f"{ce('📂', Emoji.FOLDER)} <b>Redeem Code Bot Help</b>",
        "",
        "<b>User commands:</b>",
        "<code>/start</code> - Open your dashboard and verify channels",
        "<code>/help</code> - Show this command list",
        "<code>/claim &lt;code&gt;</code> - Redeem a points claim code from an admin",
        "<code>/paysupport</code> - Get help with Telegram Stars payments",
        "",
        "<b>Dashboard options:</b>",
        f"{ce('⬇️', Emoji.DOWNLOAD)} Withdraw - Request a Google redeem code when you have enough points",
        f"{ce('🌐', Emoji.GLOBE)} Referral - Get your invite link and referral stats",
        f"{ce('📲', Emoji.PHONE)} Support Developer - Send a voluntary Telegram Stars donation",
    ]
    if settings.is_admin(telegram_user.id if telegram_user else None):
        lines.extend(
            [
                "",
                "<b>Admin commands:</b>",
                "<code>/admin</code> - Show the admin command menu",
                "<code>/stats</code> - Show bot statistics",
                "<code>/broadcast &lt;message&gt;</code> - Send a message to all registered users",
                "<code>/genpoints &lt;points&gt; [max_uses] [custom_code]</code> - Create a points claim code",
                "<code>/addcodes</code> - Add Google redeem code inventory",
                "<code>/withdrawals</code> - List pending withdrawal requests",
                "<code>/approve &lt;id&gt;</code> - Approve a withdrawal and send a code",
                "<code>/reject &lt;id&gt; [reason]</code> - Reject a withdrawal without deducting points",
            ]
        )
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def pay_support(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            f"{ce('📲', Emoji.PHONE)} <b>Payment support</b>\n\n"
            "If you had a problem with a Telegram Stars payment, send the admin your issue details, payment date, "
            "and Telegram charge ID if it is visible in your receipt.",
            parse_mode=ParseMode.HTML,
        )


def register_user_handlers(application) -> None:  # type: ignore[no-untyped-def]
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("claim", claim_command))
    application.add_handler(CommandHandler("paysupport", pay_support))
    application.add_handler(CallbackQueryHandler(verify, pattern="^verify$"))
    application.add_handler(CallbackQueryHandler(dashboard_home, pattern="^dashboard:home$"))
    application.add_handler(CallbackQueryHandler(withdraw_screen, pattern="^dashboard:withdraw$"))
    application.add_handler(CallbackQueryHandler(referral_screen, pattern="^dashboard:referral$"))
    application.add_handler(CallbackQueryHandler(support_developer, pattern="^dashboard:support$"))
