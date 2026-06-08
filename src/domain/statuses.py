from __future__ import annotations


class ReferralStatus:
    PENDING = "pending"
    AWARDED = "awarded"


class WithdrawalStatus:
    PENDING = "pending"
    RESERVED = "reserved"
    DELIVERY_FAILED = "delivery_failed"
    FULFILLED = "fulfilled"
    REJECTED = "rejected"

    OPEN = (PENDING, RESERVED, DELIVERY_FAILED)
    DELIVERABLE = (RESERVED, DELIVERY_FAILED)
    REJECTABLE = (PENDING, DELIVERY_FAILED)


class RedeemCodeStatus:
    AVAILABLE = "available"
    RESERVED = "reserved"
    SENT = "sent"

    ALL = (AVAILABLE, RESERVED, SENT)


class StarPaymentStatus:
    PENDING = "pending"
    PAID = "paid"


class LedgerReason:
    REFERRAL_VERIFIED = "referral_verified"
    CLAIM_CODE = "claim_code"
    WITHDRAWAL_REDEEM_CODE = "withdrawal_redeem_code"
