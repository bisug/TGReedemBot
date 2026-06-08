from __future__ import annotations

from telegram import LabeledPrice, Update
from telegram.error import BadRequest, TelegramError
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from src.config import Settings
from src.database import Database
from src.domain import WithdrawalStatus
from src.helpers.common import (
    answer_callback,
    get_database,
    get_membership_cache,
    get_settings,
    has_required_channels,
    parse_referral_arg,
)
from src.helpers.keyboards import (
    back_keyboard,
    commands_keyboard,
    dashboard_keyboard,
    verification_keyboard,
    withdraw_keyboard,
)
from src.helpers.security import enforce_rate_limit, require_private_chat
from src.services import RedeemService
from src.utils.limits import is_valid_claim_code
from src.utils.ui import Emoji, ce, code_block, h, quote_block


async def _send_or_edit(update: Update, text: str, **kwargs) -> None:
    if update.callback_query and update.callback_query.message:
        try:
            await update.callback_query.edit_message_text(text, **kwargs)
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
    elif update.effective_message:
        await update.effective_message.reply_text(text, **kwargs)


def _plain_command_list(lines: list[str]) -> str:
    return "\n".join(f"- {h(line)}" for line in lines)


def _is_admin(update: Update, settings: Settings) -> bool:
    telegram_user = update.effective_user
    return settings.is_admin(telegram_user.id if telegram_user else None)


def _help_text(*, is_admin: bool) -> str:
    lines = [
        f"{ce('ℹ️', Emoji.INFO)} <b>Help</b>",
        "",
        quote_block(
            "Earn points from verified referrals and claim codes. Use your points to request Google redeem codes."
        ),
        "",
        "<b>What you can do:</b>",
        f"{ce('✅', Emoji.SUCCESS)} Verify required channels",
        f"{ce('🔗', Emoji.LINK)} Share your referral link",
        f"{ce('🪙', Emoji.COIN)} Claim admin points codes",
        f"{ce('💳', Emoji.CREDIT_CARD)} Request a redeem code withdrawal",
        f"{ce('❤️', Emoji.HEART)} Support the developer with Telegram Stars",
        "",
        "Use the Commands button to see exact command formats.",
    ]
    if is_admin:
        lines.extend(
            [
                "",
                f"{ce('⚙️', Emoji.GEAR)} <b>Admin access enabled</b>",
                quote_block("Open the Admin Panel from the dashboard to manage stock, users, and withdrawals."),
            ]
        )
    return "\n".join(lines)


def _commands_intro_text(*, is_admin: bool) -> str:
    lines = [
        f"{ce('📋', Emoji.CLIPBOARD)} <b>Commands</b>",
        "",
        quote_block("Choose a command section below. Admin commands are visible only to configured admins."),
        "",
        f"{ce('📄', Emoji.DOCUMENT)} User Commands - available to everyone",
    ]
    if is_admin:
        lines.append(f"{ce('⚙️', Emoji.GEAR)} Admin Commands - visible only to configured admins")
    return "\n".join(lines)


def _user_commands_text() -> str:
    commands = _plain_command_list(
        [
            "/start - Open the dashboard",
            "/help - Open help",
            "/claim <code> - Redeem a points claim code",
            "/withdraw - Check withdrawal status and request a code",
            "/paysupport - Get help with Telegram Stars payments",
        ]
    )
    return "\n".join(
        [
            f"{ce('📄', Emoji.DOCUMENT)} <b>User Commands</b>",
            "",
            commands,
            "",
            "<b>Dashboard buttons:</b>",
            f"{ce('💳', Emoji.CREDIT_CARD)} Withdraw - Request a Google redeem code",
            f"{ce('🫂', Emoji.PEOPLE_HUGGING)} Referral - Get your invite link and stats",
            f"{ce('❤️', Emoji.HEART)} Support Developer - Send Telegram Stars support",
            f"{ce('ℹ️', Emoji.INFO)} Help - Learn how the bot works",
            f"{ce('📋', Emoji.CLIPBOARD)} Commands - View user/admin command sections",
        ]
    )


def _admin_commands_text() -> str:
    commands = _plain_command_list(
        [
            "/admin - Show the admin menu",
            "/stats - Show users, points, stock, withdrawals, and Stars totals",
            "/broadcast <message> - Send a message to all registered users",
            "Reply /broadcast - Broadcast the replied message",
            "?broadcast <message> - Alternate broadcast prefix",
            "/genpoints <points> [max_uses] [custom_code] - Create a claim code for points",
            "/addcodes - Add Google redeem code inventory, one code per line",
            "/codes [all|unused|pending|used] - List redeem codes",
            "/updatecode <old_code> <new_code> - Replace an unused redeem code",
            "/removecode <code_or_id> - Remove an unused redeem code",
            "/stock - Show redeem code stock counts",
            "/withdrawals - List open withdrawal records",
            "/approve <withdrawal_id> - Retry or manually approve a withdrawal",
            "/reject <withdrawal_id> [reason] - Reject without deducting points",
        ]
    )
    return "\n".join(
        [
            f"{ce('⚙️', Emoji.GEAR)} <b>Admin Commands</b>",
            "",
            commands,
        ]
    )


async def _require_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not await require_private_chat(update):
        return False
    if not await enforce_rate_limit(update, context, "admin_callback", limit=30, window_seconds=60):
        return False

    settings = get_settings(context)
    if _is_admin(update, settings):
        await answer_callback(update)
        return True
    if update.callback_query:
        await update.callback_query.answer("Only admins can open this section.", show_alert=True)
    return False


async def _show_verification(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    if settings.required_channel_ids:
        channel_lines = "\n".join(
            f"{index}. {channel.label}" for index, channel in enumerate(settings.required_channels(), start=1)
        )
        text = (
            f"{ce('✅', Emoji.SUCCESS)} <b>Verification Required</b>\n\n"
            f"{quote_block('Join every required chat below, then press I joined so I can verify your membership.')}\n\n"
            f"<b>Required channel(s):</b>\n{quote_block(channel_lines)}"
        )
    else:
        text = f"{ce('✅', Emoji.SUCCESS)} <b>Verification</b>\n\nPress I joined to continue to your dashboard."
    await _send_or_edit(update, text, reply_markup=verification_keyboard(settings), parse_mode=ParseMode.HTML)


async def _has_required_membership(
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    telegram_id: int,
) -> bool:
    return await has_required_channels(
        context.bot,
        settings,
        telegram_id,
        cache=get_membership_cache(context),
    )


async def _ensure_verified_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    settings = get_settings(context)
    db = get_database(context)
    telegram_user = update.effective_user
    if telegram_user is None:
        return False

    if not await _has_required_membership(context, settings, telegram_user.id):
        await _show_verification(update, context)
        return False

    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            user = await service.get_or_create_user(
                telegram_id=telegram_user.id,
                username=telegram_user.username,
                first_name=telegram_user.first_name,
            )
            await service.mark_verified_and_award(telegram_user.id, user=user)
    return True


async def missing_join_link(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query:
        await update.callback_query.answer(
            "Invite link is unavailable. The bot must be admin in that chat with invite-user permission.",
            show_alert=True,
        )


async def _show_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE, *, intro: str | None = None) -> None:
    db = get_database(context)
    settings = get_settings(context)
    telegram_user = update.effective_user
    if telegram_user is None:
        return
    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            user = await service.get_or_create_user(
                telegram_id=telegram_user.id,
                username=telegram_user.username,
                first_name=telegram_user.first_name,
            )
            points = user.point_balance
    dashboard_text = (
        f"{ce('💎', Emoji.DIAMOND)} <b>Redeem Code Dashboard</b>\n\n"
        f"<b>Available points:</b> {points}\n"
        f"<b>Withdrawal requirement:</b> {settings.withdraw_cost_points} points\n\n"
        f"{quote_block('Earn points from referrals and claim codes. When you have enough points, request a Google redeem code.')}\n\n"
        "Use Help for a quick guide or Commands for exact command formats."
    )
    await _send_or_edit(
        update,
        f"{intro}\n\n{dashboard_text}" if intro else dashboard_text,
        reply_markup=dashboard_keyboard(is_admin=_is_admin(update, settings)),
        parse_mode=ParseMode.HTML,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private_chat(update):
        return
    if not await enforce_rate_limit(update, context, "start", limit=8, window_seconds=60):
        return

    settings = get_settings(context)
    db = get_database(context)
    telegram_user = update.effective_user
    if telegram_user is None:
        return

    referral_telegram_id = parse_referral_arg(context.args)
    channel_ok = await _has_required_membership(context, settings, telegram_user.id)

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
    await _show_dashboard(
        update,
        context,
        intro=(
            f"{ce('💎', Emoji.DIAMOND)} <b>Welcome to Redeem Code Bot</b>\n\n"
            f"{quote_block('Use the dashboard below to check points, invite friends, request withdrawals, view help, or open commands.')}"
        ),
    )


async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private_chat(update):
        return
    if not await enforce_rate_limit(update, context, "verify", limit=6, window_seconds=60):
        return
    await answer_callback(update)
    if not await _ensure_verified_user(update, context):
        return
    await _show_dashboard(update, context)


async def dashboard_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private_chat(update):
        return
    if not await enforce_rate_limit(update, context, "dashboard", limit=30, window_seconds=60):
        return
    await answer_callback(update)
    if not await _ensure_verified_user(update, context):
        return
    await _show_dashboard(update, context)


async def help_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private_chat(update):
        return
    if not await enforce_rate_limit(update, context, "screen", limit=30, window_seconds=60):
        return
    await answer_callback(update)
    settings = get_settings(context)
    await _send_or_edit(
        update,
        _help_text(is_admin=_is_admin(update, settings)),
        reply_markup=back_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def commands_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private_chat(update):
        return
    if not await enforce_rate_limit(update, context, "screen", limit=30, window_seconds=60):
        return
    await answer_callback(update)
    settings = get_settings(context)
    is_admin = _is_admin(update, settings)
    await _send_or_edit(
        update,
        _commands_intro_text(is_admin=is_admin),
        reply_markup=commands_keyboard(is_admin=is_admin),
        parse_mode=ParseMode.HTML,
    )


async def user_commands_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private_chat(update):
        return
    if not await enforce_rate_limit(update, context, "screen", limit=30, window_seconds=60):
        return
    await answer_callback(update)
    await _send_or_edit(
        update,
        _user_commands_text(),
        reply_markup=back_keyboard("dashboard:commands"),
        parse_mode=ParseMode.HTML,
    )


async def admin_commands_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin_callback(update, context):
        return
    await _send_or_edit(
        update,
        _admin_commands_text(),
        reply_markup=back_keyboard("dashboard:commands"),
        parse_mode=ParseMode.HTML,
    )


async def admin_panel_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin_callback(update, context):
        return
    await _send_or_edit(
        update,
        _admin_commands_text(),
        reply_markup=back_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def referral_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private_chat(update):
        return
    if not await enforce_rate_limit(update, context, "referral", limit=20, window_seconds=60):
        return
    await answer_callback(update)
    if not await _ensure_verified_user(update, context):
        return
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
            f"{ce('🫂', Emoji.PEOPLE_HUGGING)} <b>Referral Program</b>\n\n"
            f"<b>Referral points:</b> {referral_points}\n"
            f"<b>Successful referrals:</b> {successful_referrals}\n\n"
            f"{quote_block('Share this link with friends. You earn points after they start the bot and pass channel verification.')}\n\n"
            f"<b>Your referral link:</b>\n{quote_block(link)}"
        ),
        reply_markup=back_keyboard(),
        disable_web_page_preview=True,
        parse_mode=ParseMode.HTML,
    )


def _withdrawal_status_hint(status: str) -> str:
    if status == WithdrawalStatus.PENDING:
        return "Your request is waiting for delivery."
    if status == WithdrawalStatus.RESERVED:
        return "Your redeem code is being prepared for delivery."
    if status == WithdrawalStatus.DELIVERY_FAILED:
        return "Delivery failed. An admin can retry delivery or reject the request."
    if status == WithdrawalStatus.FULFILLED:
        return "Your last withdrawal was completed."
    if status == WithdrawalStatus.REJECTED:
        return "Your last withdrawal was rejected. Your points were not deducted."
    return "Check back here for the latest withdrawal status."


async def _reserve_automatic_withdrawal(*, db: Database, settings: Settings, telegram_id: int):
    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            return await service.reserve_automatic_withdrawal(telegram_id)


async def _send_withdrawal_code(context: ContextTypes.DEFAULT_TYPE, *, telegram_id: int, code: str) -> None:
    await context.bot.send_message(
        chat_id=telegram_id,
        text=(
            f"{ce('✅', Emoji.SUCCESS)} <b>Your withdrawal was approved.</b>\n\n"
            f"<b>Google redeem code:</b>\n{code_block(code)}\n"
            f"{quote_block('Keep this code private and redeem it from your Google account.')}"
        ),
        parse_mode=ParseMode.HTML,
    )


async def _finalize_automatic_withdrawal(
    *,
    db: Database,
    settings: Settings,
    withdrawal_id: int,
):
    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            return await service.finalize_reserved_withdrawal(withdrawal_id, admin_telegram_id=None)


async def _mark_automatic_withdrawal_delivery_failed(
    *,
    db: Database,
    settings: Settings,
    withdrawal_id: int,
    error: TelegramError,
):
    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            return await service.mark_withdrawal_delivery_failed(
                withdrawal_id,
                admin_telegram_id=None,
                reason=str(error),
            )


async def _show_withdrawal_screen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    create_request: bool = False,
) -> None:
    settings = get_settings(context)
    db = get_database(context)
    telegram_user = update.effective_user
    if telegram_user is None:
        return

    result_message: str | None = None
    if create_request:
        result = await _reserve_automatic_withdrawal(db=db, settings=settings, telegram_id=telegram_user.id)
        result_message = result.message
        if result.success and result.withdrawal is not None and result.code:
            try:
                await _send_withdrawal_code(context, telegram_id=telegram_user.id, code=result.code)
            except TelegramError as exc:
                result = await _mark_automatic_withdrawal_delivery_failed(
                    db=db,
                    settings=settings,
                    withdrawal_id=result.withdrawal.id,
                    error=exc,
                )
            else:
                result = await _finalize_automatic_withdrawal(
                    db=db,
                    settings=settings,
                    withdrawal_id=result.withdrawal.id,
                )
            result_message = result.message

    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            user = await service.get_user_by_telegram_id(telegram_user.id)
            if user is None:
                await _show_verification(update, context)
                return
            latest = await service.latest_withdrawal_for_user(user.id)
            points = user.point_balance

    has_open_request = latest is not None and latest.status in WithdrawalStatus.OPEN
    can_request = points >= settings.withdraw_cost_points and not has_open_request
    points_needed = max(settings.withdraw_cost_points - points, 0)

    if latest is None:
        status_line = "No request yet"
        status_hint = "You can request a Google redeem code when you have enough points."
    else:
        status_line = f"#{latest.id} - {latest.status}"
        status_hint = _withdrawal_status_hint(latest.status)

    if result_message:
        guidance = result_message
    elif has_open_request:
        guidance = status_hint
    elif can_request:
        guidance = "You have enough points. Press Request Code to receive a redeem code automatically."
    else:
        guidance = f"You need {points_needed} more point(s). Use referrals or claim codes, then return here."

    await _send_or_edit(
        update,
        (
            f"{ce('💳', Emoji.CREDIT_CARD)} <b>Withdraw Google Redeem Code</b>\n\n"
            f"<b>Available points:</b> {points}\n"
            f"<b>Required points:</b> {settings.withdraw_cost_points}\n"
            f"<b>Withdrawal status:</b> {h(status_line)}\n\n"
            f"{quote_block(guidance)}"
        ),
        reply_markup=withdraw_keyboard(can_request=can_request, show_referral=not can_request and not has_open_request),
        parse_mode=ParseMode.HTML,
    )


async def withdraw_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private_chat(update):
        return
    if not await enforce_rate_limit(update, context, "withdraw", limit=5, window_seconds=60):
        return
    await answer_callback(update)
    if not await _ensure_verified_user(update, context):
        return
    await _show_withdrawal_screen(update, context)


async def withdraw_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private_chat(update):
        return
    if not await enforce_rate_limit(update, context, "withdraw_request", limit=3, window_seconds=60):
        return
    await answer_callback(update)
    if not await _ensure_verified_user(update, context):
        return
    await _show_withdrawal_screen(update, context, create_request=True)


async def support_developer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private_chat(update):
        return
    if not await enforce_rate_limit(update, context, "support", limit=4, window_seconds=300):
        return
    await answer_callback(update)
    if not await _ensure_verified_user(update, context):
        return
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
    if not await require_private_chat(update):
        return
    if not await enforce_rate_limit(update, context, "claim", limit=10, window_seconds=60):
        return

    telegram_user = update.effective_user
    if telegram_user is None or update.effective_message is None:
        return
    if not context.args:
        await update.effective_message.reply_text(
            f"{ce('🪙', Emoji.COIN)} <b>Claim Points</b>\n\n"
            "Please send the code you want to claim.\n\n"
            f"<b>Example:</b>\n{code_block('/claim BONUS10')}",
            parse_mode=ParseMode.HTML,
        )
        return

    settings = get_settings(context)
    db = get_database(context)
    claim_code = context.args[0]
    if not is_valid_claim_code(claim_code):
        await update.effective_message.reply_text(
            f"{ce('⚠️', Emoji.WARNING)} <b>Invalid claim code.</b>\n\n"
            f"{quote_block('Use only letters, numbers, underscores, and dashes.')}",
            parse_mode=ParseMode.HTML,
        )
        return
    channel_ok = await _has_required_membership(context, settings, telegram_user.id)

    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            user = await service.get_or_create_user(
                telegram_id=telegram_user.id,
                username=telegram_user.username,
                first_name=telegram_user.first_name,
            )
            if channel_ok:
                user = await service.mark_verified_and_award(telegram_user.id, user=user)
                result = await service.claim_points(
                    telegram_id=telegram_user.id,
                    username=telegram_user.username,
                    first_name=telegram_user.first_name,
                    code=claim_code,
                    user=user,
                )
            else:
                result = None

    if not channel_ok:
        await _show_verification(update, context)
        return
    result_message = result.message if result else "Claim failed. Please try again."
    await update.effective_message.reply_text(
        f"{ce('✅', Emoji.SUCCESS)} <b>Claim Result</b>\n\n{quote_block(result_message)}",
        parse_mode=ParseMode.HTML,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private_chat(update):
        return
    if not await enforce_rate_limit(update, context, "help", limit=20, window_seconds=60):
        return

    if update.effective_message is None:
        return
    settings = get_settings(context)
    is_admin = _is_admin(update, settings)
    await update.effective_message.reply_text(
        _help_text(is_admin=is_admin),
        reply_markup=commands_keyboard(is_admin=is_admin),
        parse_mode=ParseMode.HTML,
    )


async def pay_support(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private_chat(update):
        return

    if update.effective_message:
        await update.effective_message.reply_text(
            f"{ce('🎧', Emoji.HEADPHONES)} <b>Payment support</b>\n\n"
            f"{quote_block('If you had a problem with a Telegram Stars payment, send the admin your issue details, payment date, and Telegram charge ID if it is visible in your receipt.')}",
            parse_mode=ParseMode.HTML,
        )


def register_user_handlers(application: Application) -> None:
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("claim", claim_command))
    application.add_handler(CommandHandler("withdraw", withdraw_screen))
    application.add_handler(CommandHandler("paysupport", pay_support))
    application.add_handler(CallbackQueryHandler(verify, pattern="^verify$"))
    application.add_handler(CallbackQueryHandler(missing_join_link, pattern="^join:missing:\\d+$"))
    application.add_handler(CallbackQueryHandler(dashboard_home, pattern="^dashboard:home$"))
    application.add_handler(CallbackQueryHandler(help_screen, pattern="^dashboard:help$"))
    application.add_handler(CallbackQueryHandler(commands_screen, pattern="^dashboard:commands$"))
    application.add_handler(CallbackQueryHandler(admin_panel_screen, pattern="^dashboard:admin$"))
    application.add_handler(CallbackQueryHandler(user_commands_screen, pattern="^commands:user$"))
    application.add_handler(CallbackQueryHandler(admin_commands_screen, pattern="^commands:admin$"))
    application.add_handler(CallbackQueryHandler(withdraw_screen, pattern="^dashboard:withdraw$"))
    application.add_handler(CallbackQueryHandler(withdraw_request, pattern="^dashboard:withdraw:request$"))
    application.add_handler(CallbackQueryHandler(referral_screen, pattern="^dashboard:referral$"))
    application.add_handler(CallbackQueryHandler(support_developer, pattern="^dashboard:support$"))
