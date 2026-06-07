from __future__ import annotations

import secrets
import string
from dataclasses import dataclass
from datetime import timezone
from uuid import uuid4

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import (
    ClaimCode,
    ClaimRedemption,
    PointLedger,
    RedeemCode,
    Referral,
    StarPayment,
    User,
    Withdrawal,
    utcnow,
)
from src.settings import Settings


@dataclass(frozen=True, slots=True)
class WithdrawalRequestResult:
    status: str
    withdrawal: Withdrawal | None
    message: str


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    success: bool
    withdrawal: Withdrawal | None
    code: str | None
    message: str


@dataclass(frozen=True, slots=True)
class ClaimResult:
    success: bool
    points: int
    message: str


class RedeemService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    async def get_user_by_telegram_id(self, telegram_id: int) -> User | None:
        return await self.session.scalar(select(User).where(User.telegram_id == telegram_id))

    async def get_or_create_user(
        self,
        *,
        telegram_id: int,
        username: str | None = None,
        first_name: str | None = None,
        referral_telegram_id: int | None = None,
    ) -> User:
        user = await self.get_user_by_telegram_id(telegram_id)
        created = False
        if user is None:
            user = User(telegram_id=telegram_id)
            self.session.add(user)
            created = True

        user.username = username
        user.first_name = first_name
        await self.session.flush()

        if created and referral_telegram_id and referral_telegram_id != telegram_id:
            referrer = await self.get_user_by_telegram_id(referral_telegram_id)
            if referrer is not None:
                user.referred_by_user_id = referrer.id
                self.session.add(Referral(referrer_user_id=referrer.id, referred_user_id=user.id))
                await self.session.flush()

        return user

    async def grant_points(
        self,
        *,
        user_id: int,
        points: int,
        reason: str,
        idempotency_key: str,
        related_type: str | None = None,
        related_id: int | None = None,
    ) -> bool:
        existing = await self.session.scalar(select(PointLedger).where(PointLedger.idempotency_key == idempotency_key))
        if existing is not None:
            return False

        user = await self.session.get(User, user_id)
        if user is None:
            raise ValueError("User not found")

        self.session.add(
            PointLedger(
                user_id=user_id,
                points=points,
                reason=reason,
                related_type=related_type,
                related_id=related_id,
                idempotency_key=idempotency_key,
            )
        )
        user.point_balance += points
        await self.session.flush()
        return True

    async def mark_verified_and_award(self, telegram_id: int) -> User:
        user = await self.get_user_by_telegram_id(telegram_id)
        if user is None:
            raise ValueError("User not found")

        if not user.is_verified:
            user.is_verified = True

        referral = await self.session.scalar(
            select(Referral).where(Referral.referred_user_id == user.id, Referral.status == "pending")
        )
        if referral is not None:
            await self.grant_points(
                user_id=referral.referrer_user_id,
                points=self.settings.referral_reward_points,
                reason="referral_verified",
                related_type="referral",
                related_id=referral.id,
                idempotency_key=f"referral:{referral.id}:verified",
            )
            referral.status = "awarded"
            referral.awarded_at = utcnow()

        await self.session.flush()
        return user

    async def referral_stats(self, user_id: int) -> tuple[int, int]:
        points = await self.session.scalar(
            select(func.coalesce(func.sum(PointLedger.points), 0)).where(
                PointLedger.user_id == user_id,
                PointLedger.reason == "referral_verified",
                PointLedger.points > 0,
            )
        )
        successful = await self.session.scalar(
            select(func.count()).select_from(Referral).where(
                Referral.referrer_user_id == user_id,
                Referral.status == "awarded",
            )
        )
        return int(points or 0), int(successful or 0)

    @staticmethod
    def normalize_claim_code(code: str) -> str:
        return code.strip().upper()

    @staticmethod
    def random_claim_code(length: int = 10) -> str:
        alphabet = string.ascii_uppercase + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(length))

    async def generate_claim_code(
        self,
        *,
        points: int,
        max_redemptions: int,
        admin_telegram_id: int,
        code: str | None = None,
    ) -> ClaimCode:
        if points <= 0:
            raise ValueError("points must be greater than 0")
        if max_redemptions <= 0:
            raise ValueError("max_redemptions must be greater than 0")

        candidate = self.normalize_claim_code(code) if code else ""
        for _ in range(20):
            if not candidate:
                candidate = self.random_claim_code()
            existing = await self.session.scalar(select(ClaimCode).where(ClaimCode.code == candidate))
            if existing is None:
                claim_code = ClaimCode(
                    code=candidate,
                    points=points,
                    max_redemptions=max_redemptions,
                    created_by_admin_id=admin_telegram_id,
                )
                self.session.add(claim_code)
                await self.session.flush()
                return claim_code
            if code:
                raise ValueError("claim code already exists")
            candidate = ""
        raise ValueError("could not generate a unique claim code")

    async def claim_points(
        self,
        *,
        telegram_id: int,
        code: str,
        username: str | None = None,
        first_name: str | None = None,
    ) -> ClaimResult:
        user = await self.get_or_create_user(telegram_id=telegram_id, username=username, first_name=first_name)
        normalized = self.normalize_claim_code(code)
        claim_code = await self.session.scalar(select(ClaimCode).where(ClaimCode.code == normalized))
        if claim_code is None or not claim_code.is_active:
            return ClaimResult(False, 0, "That claim code is invalid. Please check the code and try again.")
        if claim_code.redeemed_count >= claim_code.max_redemptions:
            return ClaimResult(False, 0, "That claim code has already reached its maximum number of uses.")

        existing = await self.session.scalar(
            select(ClaimRedemption).where(
                ClaimRedemption.claim_code_id == claim_code.id,
                ClaimRedemption.user_id == user.id,
            )
        )
        if existing is not None:
            return ClaimResult(False, 0, "You have already claimed this code.")

        redemption = ClaimRedemption(claim_code_id=claim_code.id, user_id=user.id)
        self.session.add(redemption)
        await self.session.flush()
        await self.grant_points(
            user_id=user.id,
            points=claim_code.points,
            reason="claim_code",
            related_type="claim_code",
            related_id=claim_code.id,
            idempotency_key=f"claim:{claim_code.id}:user:{user.id}",
        )
        claim_code.redeemed_count += 1
        await self.session.flush()
        return ClaimResult(True, claim_code.points, f"Success. {claim_code.points} point(s) were added to your account.")

    async def admin_stats(self) -> dict[str, int]:
        total_users = await self.session.scalar(select(func.count()).select_from(User))
        verified_users = await self.session.scalar(select(func.count()).select_from(User).where(User.is_verified.is_(True)))
        pending_withdrawals = await self.session.scalar(
            select(func.count()).select_from(Withdrawal).where(Withdrawal.status == "pending")
        )
        available_codes = await self.session.scalar(
            select(func.count()).select_from(RedeemCode).where(RedeemCode.status == "available")
        )
        sent_codes = await self.session.scalar(
            select(func.count()).select_from(RedeemCode).where(RedeemCode.status == "sent")
        )
        total_points = await self.session.scalar(select(func.coalesce(func.sum(User.point_balance), 0)))
        paid_stars = await self.session.scalar(
            select(func.coalesce(func.sum(StarPayment.amount), 0)).where(StarPayment.status == "paid")
        )
        active_claim_codes = await self.session.scalar(
            select(func.count()).select_from(ClaimCode).where(ClaimCode.is_active.is_(True))
        )
        return {
            "total_users": int(total_users or 0),
            "verified_users": int(verified_users or 0),
            "pending_withdrawals": int(pending_withdrawals or 0),
            "available_codes": int(available_codes or 0),
            "sent_codes": int(sent_codes or 0),
            "total_points": int(total_points or 0),
            "paid_stars": int(paid_stars or 0),
            "active_claim_codes": int(active_claim_codes or 0),
        }

    async def all_user_telegram_ids(self) -> list[int]:
        rows = await self.session.execute(select(User.telegram_id).order_by(User.id.asc()))
        return [int(row[0]) for row in rows.all()]

    async def create_withdrawal_request(self, telegram_id: int) -> WithdrawalRequestResult:
        user = await self.get_user_by_telegram_id(telegram_id)
        if user is None:
            return WithdrawalRequestResult("missing_user", None, "Please use /start first so I can create your account.")

        existing = await self.session.scalar(
            select(Withdrawal)
            .where(Withdrawal.user_id == user.id, Withdrawal.status == "pending")
            .order_by(Withdrawal.id.desc())
        )
        if existing is not None:
            return WithdrawalRequestResult(
                "pending",
                existing,
                "You already have a withdrawal request waiting for admin review.",
            )

        if user.point_balance < self.settings.withdraw_cost_points:
            return WithdrawalRequestResult(
                "insufficient_points",
                None,
                f"You need at least {self.settings.withdraw_cost_points} points before you can request a code.",
            )

        withdrawal = Withdrawal(user_id=user.id, points_cost=self.settings.withdraw_cost_points, status="pending")
        self.session.add(withdrawal)
        await self.session.flush()
        return WithdrawalRequestResult(
            "created",
            withdrawal,
            "Your withdrawal request was created. An admin will review it and send a code if approved.",
        )

    async def latest_withdrawal_for_user(self, user_id: int) -> Withdrawal | None:
        return await self.session.scalar(
            select(Withdrawal).where(Withdrawal.user_id == user_id).order_by(Withdrawal.id.desc()).limit(1)
        )

    async def add_codes(self, codes: list[str], *, note: str | None = None) -> tuple[int, int]:
        added = 0
        skipped = 0
        for raw_code in codes:
            code = raw_code.strip()
            if not code:
                continue
            exists = await self.session.scalar(select(RedeemCode).where(RedeemCode.code == code))
            if exists is not None:
                skipped += 1
                continue
            self.session.add(RedeemCode(code=code, note=note))
            added += 1
        await self.session.flush()
        return added, skipped

    async def stock_counts(self) -> dict[str, int]:
        rows = await self.session.execute(select(RedeemCode.status, func.count()).group_by(RedeemCode.status))
        counts = {"available": 0, "reserved": 0, "sent": 0}
        for status, count in rows.all():
            counts[str(status)] = int(count)
        return counts

    async def pending_withdrawals(self, *, limit: int = 20) -> list[tuple[Withdrawal, User]]:
        statement: Select[tuple[Withdrawal, User]] = (
            select(Withdrawal, User)
            .join(User, User.id == Withdrawal.user_id)
            .where(Withdrawal.status == "pending")
            .order_by(Withdrawal.id.asc())
            .limit(limit)
        )
        rows = await self.session.execute(statement)
        return list(rows.all())

    async def approve_withdrawal(self, withdrawal_id: int, *, admin_telegram_id: int) -> ApprovalResult:
        withdrawal = await self.session.get(Withdrawal, withdrawal_id)
        if withdrawal is None:
            return ApprovalResult(False, None, None, "Withdrawal not found.")

        if withdrawal.status == "fulfilled" and withdrawal.redeem_code_id is not None:
            code = await self.session.get(RedeemCode, withdrawal.redeem_code_id)
            return ApprovalResult(True, withdrawal, code.code if code else None, "This withdrawal was already fulfilled.")

        if withdrawal.status != "pending":
            return ApprovalResult(False, withdrawal, None, f"Withdrawal is already {withdrawal.status}.")

        user = await self.session.get(User, withdrawal.user_id)
        if user is None:
            return ApprovalResult(False, withdrawal, None, "Withdrawal user not found.")
        if user.point_balance < withdrawal.points_cost:
            return ApprovalResult(False, withdrawal, None, "User no longer has enough points.")

        code = await self.session.scalar(
            select(RedeemCode).where(RedeemCode.status == "available").order_by(RedeemCode.id.asc()).limit(1)
        )
        if code is None:
            return ApprovalResult(False, withdrawal, None, "No redeem codes are available. Add stock with /addcodes first.")

        spent = await self.grant_points(
            user_id=user.id,
            points=-withdrawal.points_cost,
            reason="withdrawal_redeem_code",
            related_type="withdrawal",
            related_id=withdrawal.id,
            idempotency_key=f"withdrawal:{withdrawal.id}:approved",
        )
        if not spent:
            return ApprovalResult(False, withdrawal, None, "Withdrawal points were already deducted.")

        now = utcnow()
        code.status = "sent"
        code.assigned_withdrawal_id = withdrawal.id
        code.assigned_at = now
        withdrawal.status = "fulfilled"
        withdrawal.redeem_code_id = code.id
        withdrawal.admin_telegram_id = admin_telegram_id
        withdrawal.fulfilled_at = now
        await self.session.flush()
        return ApprovalResult(True, withdrawal, code.code, "Withdrawal approved and code sent to the user.")

    async def reject_withdrawal(
        self, withdrawal_id: int, *, admin_telegram_id: int, reason: str | None = None
    ) -> ApprovalResult:
        withdrawal = await self.session.get(Withdrawal, withdrawal_id)
        if withdrawal is None:
            return ApprovalResult(False, None, None, "Withdrawal not found.")
        if withdrawal.status != "pending":
            return ApprovalResult(False, withdrawal, None, f"Withdrawal is already {withdrawal.status}.")

        withdrawal.status = "rejected"
        withdrawal.admin_telegram_id = admin_telegram_id
        withdrawal.rejection_reason = reason
        await self.session.flush()
        return ApprovalResult(True, withdrawal, None, "Withdrawal rejected. The user kept their points.")

    async def create_star_payment(self, telegram_id: int, *, amount: int) -> StarPayment:
        user = await self.get_user_by_telegram_id(telegram_id)
        if user is None:
            user = await self.get_or_create_user(telegram_id=telegram_id)

        payment = StarPayment(user_id=user.id, invoice_payload=f"support:{user.id}:{uuid4().hex}", amount=amount)
        self.session.add(payment)
        await self.session.flush()
        return payment

    async def validate_pre_checkout(self, payload: str, *, currency: str, total_amount: int) -> tuple[bool, str]:
        payment = await self.session.scalar(select(StarPayment).where(StarPayment.invoice_payload == payload))
        if payment is None:
            return False, "Payment request was not found. Please create a new invoice."
        if payment.status != "pending":
            return False, "This invoice has already been processed."
        if currency != "XTR":
            return False, "Only Telegram Stars are supported."
        if payment.amount != total_amount:
            return False, "Invoice amount does not match."
        return True, "OK"

    async def mark_star_paid(
        self,
        *,
        payload: str,
        currency: str,
        total_amount: int,
        telegram_payment_charge_id: str,
        provider_payment_charge_id: str | None,
    ) -> StarPayment | None:
        payment = await self.session.scalar(select(StarPayment).where(StarPayment.invoice_payload == payload))
        if payment is None:
            return None
        if payment.status == "paid":
            return payment
        if currency != "XTR" or payment.amount != total_amount:
            return None

        payment.status = "paid"
        payment.telegram_payment_charge_id = telegram_payment_charge_id
        payment.provider_payment_charge_id = provider_payment_charge_id
        payment.paid_at = utcnow().astimezone(timezone.utc)
        await self.session.flush()
        return payment
