from __future__ import annotations

import asyncio
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import monotonic

from sqlalchemy import text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker, create_async_engine

from src.database.models import Base


class Database:
    def __init__(
        self,
        database_url: str,
        *,
        echo: bool = False,
        pool_size: int = 10,
        max_overflow: int = 20,
        pool_timeout: int = 30,
        pool_pre_ping: bool = False,
    ) -> None:
        self.database_url, connect_args = prepare_asyncpg_url(database_url)
        self.engine = create_async_engine(
            self.database_url,
            echo=echo,
            future=True,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            pool_recycle=1800,
            pool_use_lifo=True,
            pool_pre_ping=pool_pre_ping,
            connect_args=connect_args,
        )
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self._lifecycle_lock_connection: AsyncConnection | None = None
        self._lifecycle_lock_id: int | None = None

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    async def init_models(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(create_missing_indexes)

    async def healthcheck(self) -> bool:
        async with self.session_factory() as session:
            result = await session.execute(text("SELECT 1"))
            return result.scalar_one() == 1

    async def acquire_lifecycle_lock(
        self,
        lock_id: int,
        *,
        wait_seconds: int,
        retry_seconds: int,
    ) -> None:
        if self._lifecycle_lock_connection is not None:
            return

        deadline = monotonic() + wait_seconds
        retry_delay = max(1, retry_seconds)

        while True:
            connection = await self.engine.connect()
            locked = await connection.scalar(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": lock_id},
            )
            if locked:
                self._lifecycle_lock_connection = connection
                self._lifecycle_lock_id = lock_id
                return

            await connection.close()
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    "Another bot worker is still holding the startup lock. "
                    "Stop the duplicate worker or increase STARTUP_LOCK_WAIT_SECONDS."
                )
            await asyncio.sleep(min(retry_delay, remaining))

    async def release_lifecycle_lock(self) -> None:
        connection = self._lifecycle_lock_connection
        lock_id = self._lifecycle_lock_id
        self._lifecycle_lock_connection = None
        self._lifecycle_lock_id = None

        if connection is None:
            return

        try:
            if lock_id is not None:
                await connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": lock_id},
                )
        finally:
            await connection.close()

    async def dispose(self) -> None:
        await self.release_lifecycle_lock()
        await self.engine.dispose()


def prepare_asyncpg_url(database_url: str) -> tuple[str, dict[str, object]]:
    url = make_url(database_url)
    if not url.drivername.startswith("postgresql+asyncpg"):
        return database_url, {}

    query = dict(url.query)
    sslmode = query.pop("sslmode", None)

    connect_args: dict[str, object] = {}
    if sslmode == "require":
        connect_args["ssl"] = _ssl_context_without_verification()
    elif sslmode == "verify-ca":
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        connect_args["ssl"] = ssl_context
    elif sslmode == "verify-full":
        connect_args["ssl"] = ssl.create_default_context()

    if sslmode is None:
        return database_url, connect_args

    cleaned_url = url.set(query=query)
    return cleaned_url.render_as_string(hide_password=False), connect_args


def _ssl_context_without_verification() -> ssl.SSLContext:
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    return ssl_context


def create_missing_indexes(connection: Connection) -> None:
    for table in Base.metadata.tables.values():
        for index in table.indexes:
            index.create(bind=connection, checkfirst=True)
