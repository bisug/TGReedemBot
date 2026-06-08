from __future__ import annotations

import re

MAX_CLAIM_CODE_LENGTH = 64
MAX_REDEEM_CODE_LENGTH = 255
MAX_REDEEM_CODES_PER_BATCH = 500
MAX_BROADCAST_LENGTH = 3900
MAX_REJECTION_REASON_LENGTH = 500
MAX_POINTS_PER_CLAIM_CODE = 1_000_000
MAX_CLAIM_CODE_REDEMPTIONS = 100_000

CLAIM_CODE_PATTERN = re.compile(r"^[A-Z0-9_-]{1,64}$")


def normalize_claim_code_input(value: str) -> str:
    return value.strip().upper()


def is_valid_claim_code(value: str) -> bool:
    return bool(CLAIM_CODE_PATTERN.fullmatch(normalize_claim_code_input(value)))


def clamp_text(value: str, max_length: int) -> str:
    return value.strip()[:max_length]
