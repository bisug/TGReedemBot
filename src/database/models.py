from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "bot_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    referred_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("bot_users.id"), nullable=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    point_balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class PointLedger(Base):
    __tablename__ = "bot_point_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True, nullable=False)
    points: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    related_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    related_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ClaimCode(Base):
    __tablename__ = "bot_claim_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    points: Mapped[int] = mapped_column(Integer, nullable=False)
    max_redemptions: Mapped[int] = mapped_column(Integer, nullable=False)
    redeemed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by_admin_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ClaimRedemption(Base):
    __tablename__ = "bot_claim_redemptions"
    __table_args__ = (UniqueConstraint("claim_code_id", "user_id", name="uq_claim_redemptions_code_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    claim_code_id: Mapped[int] = mapped_column(ForeignKey("bot_claim_codes.id"), index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Referral(Base):
    __tablename__ = "bot_referrals"
    __table_args__ = (
        UniqueConstraint("referred_user_id", name="uq_referrals_referred_user"),
        UniqueConstraint("referrer_user_id", "referred_user_id", name="uq_referrals_pair"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    referrer_user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True, nullable=False)
    referred_user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    awarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Withdrawal(Base):
    __tablename__ = "bot_withdrawals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True, nullable=False)
    points_cost: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True, nullable=False)
    redeem_code_id: Mapped[int | None] = mapped_column(ForeignKey("bot_redeem_codes.id"), nullable=True)
    admin_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RedeemCode(Base):
    __tablename__ = "bot_redeem_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="available", index=True, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    assigned_withdrawal_id: Mapped[int | None] = mapped_column(ForeignKey("bot_withdrawals.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StarPayment(Base):
    __tablename__ = "bot_star_payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True, nullable=False)
    invoice_payload: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    telegram_payment_charge_id: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    provider_payment_charge_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


Index(
    "ix_bot_users_verified",
    User.id,
    postgresql_where=User.is_verified.is_(True),
)
Index(
    "ix_bot_point_ledger_referral_stats",
    PointLedger.user_id,
    postgresql_where=(PointLedger.reason == "referral_verified") & (PointLedger.points > 0),
)
Index(
    "ix_bot_referrals_awarded_referrer",
    Referral.referrer_user_id,
    postgresql_where=Referral.status == "awarded",
)
Index(
    "ix_bot_withdrawals_user_latest",
    Withdrawal.user_id,
    Withdrawal.id.desc(),
)
Index(
    "ix_bot_withdrawals_pending_user_latest",
    Withdrawal.user_id,
    Withdrawal.id.desc(),
    postgresql_where=Withdrawal.status == "pending",
)
Index(
    "ix_bot_withdrawals_pending_queue",
    Withdrawal.id,
    postgresql_where=Withdrawal.status == "pending",
)
Index(
    "ix_bot_redeem_codes_available_queue",
    RedeemCode.id,
    postgresql_where=RedeemCode.status == "available",
)
Index(
    "ix_bot_claim_codes_active",
    ClaimCode.id,
    postgresql_where=ClaimCode.is_active.is_(True),
)
Index(
    "ix_bot_star_payments_paid_amount",
    StarPayment.amount,
    postgresql_where=StarPayment.status == "paid",
)
