from __future__ import annotations

from sqlalchemy import select

from src.models import ClaimCode, PointLedger, RedeemCode, Referral, StarPayment, User
from src.service import RedeemService


async def test_referral_awarded_after_verification(database, test_settings) -> None:
    async with database.session() as session:
        async with session.begin():
            service = RedeemService(session, test_settings)
            referrer = await service.get_or_create_user(telegram_id=100, username="referrer")
            referred = await service.get_or_create_user(
                telegram_id=200,
                username="referred",
                referral_telegram_id=100,
            )
            assert referred.referred_by_user_id == referrer.id

            referral = await session.scalar(select(Referral).where(Referral.referred_user_id == referred.id))
            assert referral is not None
            assert referral.status == "pending"
            assert referrer.point_balance == 0

            await service.mark_verified_and_award(200)
            await service.mark_verified_and_award(200)

            refreshed_referrer = await session.get(User, referrer.id)
            assert refreshed_referrer.point_balance == 1
            ledgers = (await session.execute(select(PointLedger))).scalars().all()
            assert len(ledgers) == 1


async def test_self_referral_is_ignored(database, test_settings) -> None:
    async with database.session() as session:
        async with session.begin():
            service = RedeemService(session, test_settings)
            user = await service.get_or_create_user(telegram_id=300, referral_telegram_id=300)
            referral = await session.scalar(select(Referral).where(Referral.referred_user_id == user.id))
            assert referral is None
            assert user.referred_by_user_id is None


async def test_withdrawal_requires_points_and_reuses_pending(database, test_settings) -> None:
    async with database.session() as session:
        async with session.begin():
            service = RedeemService(session, test_settings)
            user = await service.get_or_create_user(telegram_id=400)

            insufficient = await service.create_withdrawal_request(400)
            assert insufficient.status == "insufficient_points"

            await service.grant_points(
                user_id=user.id,
                points=5,
                reason="test_grant",
                idempotency_key="test:user400:grant",
            )
            created = await service.create_withdrawal_request(400)
            duplicate = await service.create_withdrawal_request(400)

            assert created.status == "created"
            assert duplicate.status == "pending"
            assert duplicate.withdrawal.id == created.withdrawal.id


async def test_admin_approval_needs_stock_and_is_idempotent(database, test_settings) -> None:
    async with database.session() as session:
        async with session.begin():
            service = RedeemService(session, test_settings)
            user = await service.get_or_create_user(telegram_id=500)
            await service.grant_points(
                user_id=user.id,
                points=5,
                reason="test_grant",
                idempotency_key="test:user500:grant",
            )
            request = await service.create_withdrawal_request(500)

            no_stock = await service.approve_withdrawal(request.withdrawal.id, admin_telegram_id=999)
            assert not no_stock.success

            await service.add_codes(["GOOGLE-CODE-1"])
            approved = await service.approve_withdrawal(request.withdrawal.id, admin_telegram_id=999)
            again = await service.approve_withdrawal(request.withdrawal.id, admin_telegram_id=999)

            assert approved.success
            assert approved.code == "GOOGLE-CODE-1"
            assert again.success
            assert again.code == "GOOGLE-CODE-1"

            refreshed_user = await session.get(User, user.id)
            code = await session.scalar(select(RedeemCode).where(RedeemCode.code == "GOOGLE-CODE-1"))
            assert refreshed_user.point_balance == 0
            assert code.status == "sent"


async def test_star_payment_validation_and_paid_marker(database, test_settings) -> None:
    async with database.session() as session:
        async with session.begin():
            service = RedeemService(session, test_settings)
            await service.get_or_create_user(telegram_id=600)
            payment = await service.create_star_payment(600, amount=10)

            ok, _ = await service.validate_pre_checkout(payment.invoice_payload, currency="XTR", total_amount=10)
            bad_currency, _ = await service.validate_pre_checkout(
                payment.invoice_payload,
                currency="USD",
                total_amount=10,
            )
            paid = await service.mark_star_paid(
                payload=payment.invoice_payload,
                currency="XTR",
                total_amount=10,
                telegram_payment_charge_id="tg-charge-1",
                provider_payment_charge_id="provider-charge-1",
            )
            duplicate = await service.mark_star_paid(
                payload=payment.invoice_payload,
                currency="XTR",
                total_amount=10,
                telegram_payment_charge_id="tg-charge-1",
                provider_payment_charge_id="provider-charge-1",
            )

            stored = await session.scalar(select(StarPayment).where(StarPayment.invoice_payload == payment.invoice_payload))
            assert ok
            assert not bad_currency
            assert paid is not None
            assert duplicate is not None
            assert stored.status == "paid"


async def test_admin_generated_claim_code_awards_points_once_and_expires(database, test_settings) -> None:
    async with database.session() as session:
        async with session.begin():
            service = RedeemService(session, test_settings)
            claim_code = await service.generate_claim_code(
                points=3,
                max_redemptions=2,
                admin_telegram_id=999,
                code="bonus3",
            )

            first = await service.claim_points(telegram_id=700, code="BONUS3")
            duplicate = await service.claim_points(telegram_id=700, code="bonus3")
            second = await service.claim_points(telegram_id=701, code="bonus3")
            exhausted = await service.claim_points(telegram_id=702, code="bonus3")

            user = await service.get_user_by_telegram_id(700)
            stored_code = await session.scalar(select(ClaimCode).where(ClaimCode.id == claim_code.id))
            stats = await service.admin_stats()

            assert first.success
            assert first.points == 3
            assert not duplicate.success
            assert second.success
            assert not exhausted.success
            assert user.point_balance == 3
            assert stored_code.code == "BONUS3"
            assert stored_code.redeemed_count == 2
            assert stats["active_claim_codes"] == 1
            assert stats["total_users"] == 3
