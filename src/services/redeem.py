from __future__ import annotations

import secrets
import string
from dataclasses import dataclass
from datetime import timezone
from uuid import uuid4

from sqlalchemy import Select, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import Settings
from src.database.models import (
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
from src.domain import LedgerReason, RedeemCodeStatus, ReferralStatus, StarPaymentStatus, WithdrawalStatus
from src.utils.limits import (
    MAX_CLAIM_CODE_LENGTH,
    MAX_CLAIM_CODE_REDEMPTIONS,
    MAX_POINTS_PER_CLAIM_CODE,
    MAX_REDEEM_CODE_LENGTH,
    MAX_REDEEM_CODES_PER_BATCH,
    is_valid_claim_code,
    normalize_claim_code_input,
)


@dataclass(frozen=True, slots=True)
class WithdrawalRequestResult:
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

    async def get_user_for_update_by_telegram_id(self, telegram_id: int) -> User | None:
        return await self.session.scalar(
            select(User)
            .where(User.telegram_id == telegram_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def get_user_for_update_by_id(self, user_id: int) -> User | None:
        return await self.session.scalar(
            select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
        )

    async def get_or_create_user(
        self,
        *,
        telegram_id: int,
        username: str | None = None,
        first_name: str | None = None,
        referral_telegram_id: int | None = None,
    ) -> User:
        created_user_id = await self.session.scalar(
            pg_insert(User)
            .values(telegram_id=telegram_id, username=username, first_name=first_name)
            .on_conflict_do_nothing(index_elements=[User.telegram_id])
            .returning(User.id)
        )
        created = created_user_id is not None
        user = await (
            self.get_user_for_update_by_id(created_user_id)
            if created_user_id is not None
            else self.get_user_for_update_by_telegram_id(telegram_id)
        )
        if user is None:
            raise ValueError("User not found")

        needs_flush = False
        if user.username != username:
            user.username = username
            needs_flush = True
        if user.first_name != first_name:
            user.first_name = first_name
            needs_flush = True
        if needs_flush:
            await self.session.flush()

        if created and referral_telegram_id and referral_telegram_id != telegram_id:
            referrer = await self.get_user_by_telegram_id(referral_telegram_id)
            if referrer is not None:
                user.referred_by_user_id = referrer.id
                await self.session.execute(
                    pg_insert(Referral)
                    .values(referrer_user_id=referrer.id, referred_user_id=user.id)
                    .on_conflict_do_nothing()
                )
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
        inserted_ledger_id = await self.session.scalar(
            pg_insert(PointLedger)
            .values(
                user_id=user_id,
                points=points,
                reason=reason,
                related_type=related_type,
                related_id=related_id,
                idempotency_key=idempotency_key,
            )
            .on_conflict_do_nothing(index_elements=[PointLedger.idempotency_key])
            .returning(PointLedger.id)
        )
        if inserted_ledger_id is None:
            return False

        result = await self.session.execute(
            update(User).where(User.id == user_id).values(point_balance=User.point_balance + points)
        )
        if result.rowcount == 0:
            raise ValueError("User not found")
        await self.session.flush()
        return True

    async def mark_verified_and_award(self, telegram_id: int, *, user: User | None = None) -> User:
        if user is None:
            user = await self.get_user_for_update_by_telegram_id(telegram_id)
        else:
            user = await self.get_user_for_update_by_id(user.id)
        if user is None:
            raise ValueError("User not found")

        if not user.is_verified:
            user.is_verified = True

        referral = await self.session.scalar(
            select(Referral)
            .where(Referral.referred_user_id == user.id, Referral.status == ReferralStatus.PENDING)
            .with_for_update()
        )
        if referral is not None:
            await self.grant_points(
                user_id=referral.referrer_user_id,
                points=self.settings.referral_reward_points,
                reason=LedgerReason.REFERRAL_VERIFIED,
                related_type="referral",
                related_id=referral.id,
                idempotency_key=f"referral:{referral.id}:verified",
            )
            referral.status = ReferralStatus.AWARDED
            referral.awarded_at = utcnow()

        await self.session.flush()
        return user

    async def referral_stats(self, user_id: int) -> tuple[int, int]:
        statement = select(
            select(func.coalesce(func.sum(PointLedger.points), 0))
            .where(
                PointLedger.user_id == user_id,
                PointLedger.reason == LedgerReason.REFERRAL_VERIFIED,
                PointLedger.points > 0,
            )
            .scalar_subquery(),
            select(func.count(Referral.id))
            .where(
                Referral.referrer_user_id == user_id,
                Referral.status == ReferralStatus.AWARDED,
            )
            .scalar_subquery(),
        )
        points, successful = (await self.session.execute(statement)).one()
        return int(points or 0), int(successful or 0)

    @staticmethod
    def normalize_claim_code(code: str) -> str:
        return normalize_claim_code_input(code)

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
        if points > MAX_POINTS_PER_CLAIM_CODE:
            raise ValueError(f"points must be at most {MAX_POINTS_PER_CLAIM_CODE}")
        if max_redemptions <= 0:
            raise ValueError("max_redemptions must be greater than 0")
        if max_redemptions > MAX_CLAIM_CODE_REDEMPTIONS:
            raise ValueError(f"max_redemptions must be at most {MAX_CLAIM_CODE_REDEMPTIONS}")

        candidate = self.normalize_claim_code(code) if code else ""
        if candidate and not is_valid_claim_code(candidate):
            raise ValueError(
                "claim code may contain only letters, numbers, underscores, and dashes "
                f"and must be at most {MAX_CLAIM_CODE_LENGTH} characters"
            )
        for _ in range(20):
            if not candidate:
                candidate = self.random_claim_code()
            claim_code_id = await self.session.scalar(
                pg_insert(ClaimCode)
                .values(
                    code=candidate,
                    points=points,
                    max_redemptions=max_redemptions,
                    created_by_admin_id=admin_telegram_id,
                )
                .on_conflict_do_nothing(index_elements=[ClaimCode.code])
                .returning(ClaimCode.id)
            )
            if claim_code_id is not None:
                claim_code = await self.session.get(ClaimCode, claim_code_id)
                if claim_code is None:
                    raise ValueError("claim code was not created")
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
        user: User | None = None,
    ) -> ClaimResult:
        if user is None:
            user = await self.get_or_create_user(telegram_id=telegram_id, username=username, first_name=first_name)
        normalized = self.normalize_claim_code(code)
        if not is_valid_claim_code(normalized):
            return ClaimResult(False, 0, "That claim code format is invalid. Please check the code and try again.")
        claim_code = await self.session.scalar(
            select(ClaimCode).where(ClaimCode.code == normalized).with_for_update()
        )
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
            reason=LedgerReason.CLAIM_CODE,
            related_type="claim_code",
            related_id=claim_code.id,
            idempotency_key=f"claim:{claim_code.id}:user:{user.id}",
        )
        claim_code.redeemed_count += 1
        await self.session.flush()
        return ClaimResult(True, claim_code.points, f"Success. {claim_code.points} point(s) were added to your account.")

    async def admin_stats(self) -> dict[str, int]:
        statement = select(
            select(func.count(User.id)).scalar_subquery(),
            select(func.count(User.id)).where(User.is_verified.is_(True)).scalar_subquery(),
            select(func.count(Withdrawal.id)).where(Withdrawal.status == WithdrawalStatus.PENDING).scalar_subquery(),
            select(func.count(RedeemCode.id)).where(RedeemCode.status == RedeemCodeStatus.AVAILABLE).scalar_subquery(),
            select(func.count(RedeemCode.id)).where(RedeemCode.status == RedeemCodeStatus.SENT).scalar_subquery(),
            select(func.coalesce(func.sum(User.point_balance), 0)).scalar_subquery(),
            select(func.coalesce(func.sum(StarPayment.amount), 0))
            .where(StarPayment.status == StarPaymentStatus.PAID)
            .scalar_subquery(),
            select(func.count(ClaimCode.id)).where(ClaimCode.is_active.is_(True)).scalar_subquery(),
        )
        (
            total_users,
            verified_users,
            pending_withdrawals,
            available_codes,
            sent_codes,
            total_points,
            paid_stars,
            active_claim_codes,
        ) = (await self.session.execute(statement)).one()
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
        user = await self.get_user_for_update_by_telegram_id(telegram_id)
        if user is None:
            return WithdrawalRequestResult(None, "Please use /start first so I can create your account.")

        existing = await self.session.scalar(
            select(Withdrawal)
            .where(Withdrawal.user_id == user.id, Withdrawal.status.in_(WithdrawalStatus.OPEN))
            .order_by(Withdrawal.id.desc())
            .limit(1)
        )
        if existing is not None:
            return WithdrawalRequestResult(
                existing,
                "You already have a withdrawal request waiting for admin review or delivery.",
            )

        if user.point_balance < self.settings.withdraw_cost_points:
            return WithdrawalRequestResult(
                None,
                f"You need at least {self.settings.withdraw_cost_points} points before you can request a code.",
            )

        withdrawal = Withdrawal(
            user_id=user.id,
            points_cost=self.settings.withdraw_cost_points,
            status=WithdrawalStatus.PENDING,
        )
        self.session.add(withdrawal)
        await self.session.flush()
        return WithdrawalRequestResult(
            withdrawal,
            "Your withdrawal request was created. An admin will review it and send a code if approved.",
        )

    async def latest_withdrawal_for_user(self, user_id: int) -> Withdrawal | None:
        return await self.session.scalar(
            select(Withdrawal).where(Withdrawal.user_id == user_id).order_by(Withdrawal.id.desc()).limit(1)
        )

    async def add_codes(self, codes: list[str], *, note: str | None = None) -> tuple[int, int]:
        cleaned_codes: list[str] = []
        seen: set[str] = set()
        skipped = 0

        for raw_code in codes[:MAX_REDEEM_CODES_PER_BATCH]:
            code = raw_code.strip()
            if not code or len(code) > MAX_REDEEM_CODE_LENGTH:
                continue
            if code in seen:
                skipped += 1
                continue
            seen.add(code)
            cleaned_codes.append(code)

        if not cleaned_codes:
            return 0, skipped

        inserted_codes = await self.session.scalars(
            pg_insert(RedeemCode)
            .values([{"code": code, "note": note} for code in cleaned_codes])
            .on_conflict_do_nothing(index_elements=[RedeemCode.code])
            .returning(RedeemCode.code)
        )
        added = len(inserted_codes.all())
        skipped += len(cleaned_codes) - added
        await self.session.flush()
        return added, skipped

    async def stock_counts(self) -> dict[str, int]:
        rows = await self.session.execute(select(RedeemCode.status, func.count()).group_by(RedeemCode.status))
        counts = {status: 0 for status in RedeemCodeStatus.ALL}
        for status, count in rows.all():
            counts[str(status)] = int(count)
        return counts

    async def pending_withdrawals(self, *, limit: int = 20) -> list[tuple[Withdrawal, User]]:
        statement: Select[tuple[Withdrawal, User]] = (
            select(Withdrawal, User)
            .join(User, User.id == Withdrawal.user_id)
            .where(Withdrawal.status.in_(WithdrawalStatus.OPEN))
            .order_by(Withdrawal.id.asc())
            .limit(limit)
        )
        rows = await self.session.execute(statement)
        return list(rows.all())

    async def _get_withdrawal_for_update(self, withdrawal_id: int) -> Withdrawal | None:
        return await self.session.scalar(
            select(Withdrawal).where(Withdrawal.id == withdrawal_id).with_for_update()
        )

    async def _get_redeem_code_for_update(self, redeem_code_id: int) -> RedeemCode | None:
        return await self.session.scalar(
            select(RedeemCode).where(RedeemCode.id == redeem_code_id).with_for_update()
        )

    async def _next_available_redeem_code(self) -> RedeemCode | None:
        return await self.session.scalar(
            select(RedeemCode)
            .where(RedeemCode.status == RedeemCodeStatus.AVAILABLE)
            .order_by(RedeemCode.id.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )

    async def _reserved_withdrawal_code(self, withdrawal: Withdrawal) -> RedeemCode | None:
        if withdrawal.redeem_code_id is None:
            return None
        return await self._get_redeem_code_for_update(withdrawal.redeem_code_id)

    @staticmethod
    def _reserve_code_for_withdrawal(
        *, withdrawal: Withdrawal, code: RedeemCode, admin_telegram_id: int
    ) -> None:
        now = utcnow()
        code.status = RedeemCodeStatus.RESERVED
        code.assigned_withdrawal_id = withdrawal.id
        code.assigned_at = now
        withdrawal.status = WithdrawalStatus.RESERVED
        withdrawal.redeem_code_id = code.id
        withdrawal.admin_telegram_id = admin_telegram_id

    @staticmethod
    def _release_reserved_code(code: RedeemCode) -> None:
        code.status = RedeemCodeStatus.AVAILABLE
        code.assigned_withdrawal_id = None
        code.assigned_at = None

    async def reserve_withdrawal_approval(self, withdrawal_id: int, *, admin_telegram_id: int) -> ApprovalResult:
        withdrawal = await self._get_withdrawal_for_update(withdrawal_id)
        if withdrawal is None:
            return ApprovalResult(False, None, None, "Withdrawal not found.")

        if withdrawal.status == WithdrawalStatus.FULFILLED and withdrawal.redeem_code_id is not None:
            code = await self.session.get(RedeemCode, withdrawal.redeem_code_id)
            return ApprovalResult(
                True,
                withdrawal,
                code.code if code else None,
                "This withdrawal was already fulfilled.",
            )

        if withdrawal.status in WithdrawalStatus.DELIVERABLE:
            code = await self._reserved_withdrawal_code(withdrawal)
            if code is None:
                return ApprovalResult(False, withdrawal, None, "Reserved redeem code was not found.")
            code.status = RedeemCodeStatus.RESERVED
            withdrawal.status = WithdrawalStatus.RESERVED
            withdrawal.admin_telegram_id = admin_telegram_id
            await self.session.flush()
            return ApprovalResult(True, withdrawal, code.code, "Withdrawal delivery retry prepared.")

        if withdrawal.status != WithdrawalStatus.PENDING:
            return ApprovalResult(False, withdrawal, None, f"Withdrawal is already {withdrawal.status}.")

        user = await self.get_user_for_update_by_id(withdrawal.user_id)
        if user is None:
            return ApprovalResult(False, withdrawal, None, "Withdrawal user not found.")
        if user.point_balance < withdrawal.points_cost:
            return ApprovalResult(False, withdrawal, None, "User no longer has enough points.")

        code = await self._next_available_redeem_code()
        if code is None:
            return ApprovalResult(
                False,
                withdrawal,
                None,
                "No redeem codes are available. Add stock with /addcodes first.",
            )

        self._reserve_code_for_withdrawal(withdrawal=withdrawal, code=code, admin_telegram_id=admin_telegram_id)
        await self.session.flush()
        return ApprovalResult(True, withdrawal, code.code, "Withdrawal reserved. Sending code to the user.")

    async def finalize_reserved_withdrawal(self, withdrawal_id: int, *, admin_telegram_id: int) -> ApprovalResult:
        withdrawal = await self._get_withdrawal_for_update(withdrawal_id)
        if withdrawal is None:
            return ApprovalResult(False, None, None, "Withdrawal not found.")
        if withdrawal.status == WithdrawalStatus.FULFILLED and withdrawal.redeem_code_id is not None:
            code = await self.session.get(RedeemCode, withdrawal.redeem_code_id)
            return ApprovalResult(
                True,
                withdrawal,
                code.code if code else None,
                "This withdrawal was already fulfilled.",
            )
        if withdrawal.status not in WithdrawalStatus.DELIVERABLE or withdrawal.redeem_code_id is None:
            return ApprovalResult(False, withdrawal, None, f"Withdrawal is already {withdrawal.status}.")

        code = await self._reserved_withdrawal_code(withdrawal)
        if code is None:
            return ApprovalResult(False, withdrawal, None, "Reserved redeem code was not found.")

        user = await self.get_user_for_update_by_id(withdrawal.user_id)
        if user is None:
            return ApprovalResult(False, withdrawal, None, "Withdrawal user not found.")
        if user.point_balance < withdrawal.points_cost:
            return ApprovalResult(False, withdrawal, None, "User no longer has enough points.")

        spent = await self.grant_points(
            user_id=user.id,
            points=-withdrawal.points_cost,
            reason=LedgerReason.WITHDRAWAL_REDEEM_CODE,
            related_type="withdrawal",
            related_id=withdrawal.id,
            idempotency_key=f"withdrawal:{withdrawal.id}:approved",
        )
        if not spent:
            return ApprovalResult(False, withdrawal, None, "Withdrawal points were already deducted.")

        now = utcnow()
        code.status = RedeemCodeStatus.SENT
        code.assigned_withdrawal_id = withdrawal.id
        code.assigned_at = now
        withdrawal.status = WithdrawalStatus.FULFILLED
        withdrawal.redeem_code_id = code.id
        withdrawal.admin_telegram_id = admin_telegram_id
        withdrawal.fulfilled_at = now
        await self.session.flush()
        return ApprovalResult(True, withdrawal, code.code, "Withdrawal approved and code sent to the user.")

    async def mark_withdrawal_delivery_failed(
        self, withdrawal_id: int, *, admin_telegram_id: int, reason: str | None = None
    ) -> ApprovalResult:
        withdrawal = await self._get_withdrawal_for_update(withdrawal_id)
        if withdrawal is None:
            return ApprovalResult(False, None, None, "Withdrawal not found.")
        if withdrawal.status != WithdrawalStatus.RESERVED:
            return ApprovalResult(False, withdrawal, None, f"Withdrawal is already {withdrawal.status}.")

        withdrawal.status = WithdrawalStatus.DELIVERY_FAILED
        withdrawal.admin_telegram_id = admin_telegram_id
        withdrawal.rejection_reason = reason
        await self.session.flush()
        return ApprovalResult(
            False,
            withdrawal,
            None,
            "Could not deliver the redeem code. The user was not charged and the reserved code can be retried.",
        )

    async def reject_withdrawal(
        self, withdrawal_id: int, *, admin_telegram_id: int, reason: str | None = None
    ) -> ApprovalResult:
        withdrawal = await self._get_withdrawal_for_update(withdrawal_id)
        if withdrawal is None:
            return ApprovalResult(False, None, None, "Withdrawal not found.")
        if withdrawal.status not in WithdrawalStatus.REJECTABLE:
            return ApprovalResult(False, withdrawal, None, f"Withdrawal is already {withdrawal.status}.")

        if withdrawal.status == WithdrawalStatus.DELIVERY_FAILED and withdrawal.redeem_code_id is not None:
            code = await self._reserved_withdrawal_code(withdrawal)
            if code is not None and code.status == RedeemCodeStatus.RESERVED:
                self._release_reserved_code(code)
            withdrawal.redeem_code_id = None

        withdrawal.status = WithdrawalStatus.REJECTED
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
        if payment.status != StarPaymentStatus.PENDING:
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
        payment = await self.session.scalar(
            select(StarPayment).where(StarPayment.invoice_payload == payload).with_for_update()
        )
        if payment is None:
            return None
        if payment.status == StarPaymentStatus.PAID:
            return payment
        if currency != "XTR" or payment.amount != total_amount:
            return None

        payment.status = StarPaymentStatus.PAID
        payment.telegram_payment_charge_id = telegram_payment_charge_id
        payment.provider_payment_charge_id = provider_payment_charge_id
        payment.paid_at = utcnow().astimezone(timezone.utc)
        await self.session.flush()
        return payment
