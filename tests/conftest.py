from __future__ import annotations

import pytest

from src.db import Database
from src.settings import Settings


@pytest.fixture
def test_settings(tmp_path) -> Settings:
    return Settings(
        bot_token="123:ABC",
        bot_username="TestRedeemBot",
        admin_ids=frozenset({999}),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        referral_reward_points=1,
        withdraw_cost_points=5,
        support_stars_amount=10,
    )


@pytest.fixture
async def database(test_settings: Settings) -> Database:
    db = Database(test_settings.database_url)
    await db.init_models()
    try:
        yield db
    finally:
        await db.dispose()
