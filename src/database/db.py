from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urlencode

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.database.models import Base


class Database:
    def __init__(self, database_url: str, *, echo: bool = False) -> None:
        self.database_url, connect_args = prepare_asyncpg_url(database_url)
        self.engine = create_async_engine(
            self.database_url,
            echo=echo,
            future=True,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    async def init_models(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def healthcheck(self) -> bool:
        async with self.session_factory() as session:
            result = await session.execute(text("SELECT 1"))
            return result.scalar_one() == 1

    async def dispose(self) -> None:
        await self.engine.dispose()


def prepare_asyncpg_url(database_url: str) -> tuple[str, dict[str, object]]:
    url = make_url(database_url)
    if not url.drivername.startswith("postgresql+asyncpg"):
        return database_url, {}

    query_pairs = parse_qsl(url.query_string(), keep_blank_values=True)
    sslmode = None
    kept_pairs: list[tuple[str, str]] = []
    for key, value in query_pairs:
        if key == "sslmode":
            sslmode = value
        else:
            kept_pairs.append((key, value))

    connect_args: dict[str, object] = {}
    if sslmode and sslmode not in {"disable", "allow", "prefer"}:
        connect_args["ssl"] = True

    if sslmode is None:
        return database_url, connect_args

    cleaned_url = url.set(query=urlencode(kept_pairs))
    return cleaned_url.render_as_string(hide_password=False), connect_args
