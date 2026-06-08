from __future__ import annotations

from telegram import LabeledPrice, Update
from telegram.error import BadRequest
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from src.config import Settings
from src.helpers.common import (
    answer_callback,
    get_database,
    get_membership_cache,
    get_settings,
    has_required_channels,
    parse_referral_arg,
)
from src.helpers.keyboards import back_keyboard, commands_keyboard, dashboard_keyboard, verification_keyboard
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
    return "\n".join(
        [
            f"{ce('📄', Emoji.DOCUMENT)} <b>User Commands</b>",
            "",
            code_block(
                "/start - Open the dashboard\n"
                "/help - Open help\n"
                "/claim <code> - Redeem a points claim code\n"
                "/paysupport - Get help with Telegram Stars payments"
            ),
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
    return "\n".join(
        [
            f"{ce('⚙️', Emoji.GEAR)} <b>Admin Commands</b>",
            "",
            code_block(
                "/admin - Show the admin menu\n"
                "/stats - Show users, points, stock, withdrawals, and Stars totals\n"
                "/broadcast <message> - Send a message to all registered users\n"
                "/genpoints <points> [max_uses] [custom_code] - Create a claim code for points\n"
                "/addcodes - Add Google redeem code inventory, one code per line\n"
                "/stock - Show redeem code stock counts\n"
                "/withdrawals - List pending withdrawal requests\n"
                "/approve <withdrawal_id> - Approve a request and send a code\n"
                "/reject <withdrawal_id> [reason] - Reject a request without deducting points"
            ),
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


async def withdraw_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private_chat(update):
        return
    if not await enforce_rate_limit(update, context, "withdraw", limit=5, window_seconds=60):
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
            result = await service.create_withdrawal_request(telegram_user.id)
            latest = result.withdrawal or await service.latest_withdrawal_for_user(user.id)
            points = user.point_balance

    status = latest.status if latest is not None else "none"
    await _send_or_edit(
        update,
        (
            f"{ce('💳', Emoji.CREDIT_CARD)} <b>Withdraw Google Redeem Code</b>\n\n"
            f"<b>Available points:</b> {points}\n"
            f"<b>Required points:</b> {settings.withdraw_cost_points}\n"
            f"<b>Withdrawal status:</b> {h(status)}\n\n"
            f"{quote_block(result.message)}"
        ),
        reply_markup=back_keyboard(),
        parse_mode=ParseMode.HTML,
    )


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
    application.add_handler(CallbackQueryHandler(referral_screen, pattern="^dashboard:referral$"))
    application.add_handler(CallbackQueryHandler(support_developer, pattern="^dashboard:support$"))
