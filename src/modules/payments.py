from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, PreCheckoutQueryHandler, filters

from src.helpers.common import get_database, get_settings
from src.helpers.security import require_private_chat
from src.services import RedeemService
from src.utils.ui import Emoji, ce, quote_block


async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    if query is None:
        return

    settings = get_settings(context)
    db = get_database(context)
    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            ok, message = await service.validate_pre_checkout(
                query.invoice_payload,
                currency=query.currency,
                total_amount=query.total_amount,
            )
    await query.answer(ok=ok, error_message=None if ok else message)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private_chat(update):
        return

    message = update.effective_message
    if message is None or message.successful_payment is None:
        return

    payment = message.successful_payment
    settings = get_settings(context)
    db = get_database(context)
    async with db.session() as session:
        async with session.begin():
            service = RedeemService(session, settings)
            stored = await service.mark_star_paid(
                payload=payment.invoice_payload,
                currency=payment.currency,
                total_amount=payment.total_amount,
                telegram_payment_charge_id=payment.telegram_payment_charge_id,
                provider_payment_charge_id=payment.provider_payment_charge_id,
            )

    if stored is None:
        await message.reply_text(
            f"{ce('📲', Emoji.PHONE)} <b>Payment needs review</b>\n\n"
            f"{quote_block('Payment was received, but I could not match it to an active invoice. Please use /paysupport.')}",
            parse_mode=ParseMode.HTML,
        )
        return
    await message.reply_text(
        f"{ce('✔️', Emoji.CHECK)} <b>Payment recorded</b>\n\n"
        f"{quote_block('Thank you for supporting the developer. Your Telegram Stars payment was recorded.')}",
        parse_mode=ParseMode.HTML,
    )


def register_payment_handlers(application: Application) -> None:
    application.add_handler(PreCheckoutQueryHandler(pre_checkout))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
