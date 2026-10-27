"""Database engine, session factory and ORM tables.

Async SQLAlchemy + asyncpg so DB calls never block the event loop that is also streaming
tokens. Schema changes go through Alembic migrations (see ``migrations/``); the tables are
defined here once and Alembic autogenerates diffs from them.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Token-bucket parameters: sustained requests/minute and burst size.
    rate_limit_rpm: Mapped[int] = mapped_column(Integer, default=60)
    rate_limit_burst: Mapped[int] = mapped_column(Integer, default=10)
    # Total (prompt + completion) tokens per calendar month (UTC).
    monthly_token_quota: Mapped[int] = mapped_column(BigInteger, default=1_000_000)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="tenant")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    # Only the hash is stored; the prefix lets humans tell keys apart in listings.
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tenant: Mapped[Tenant] = relationship(back_populates="api_keys")


class TokenUsage(Base):
    """One row per tenant per month; incremented atomically with an upsert."""

    __tablename__ = "token_usage"

    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    month: Mapped[str] = mapped_column(String(7), primary_key=True)  # "YYYY-MM"
    tokens: Mapped[int] = mapped_column(BigInteger, default=0)


class Database:
    """Owns the engine. One instance per app, created in the lifespan handler."""

    def __init__(self, url: str) -> None:
        # pool_pre_ping drops dead connections after a Postgres restart instead of erroring.
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        # expire_on_commit=False: objects stay readable after commit (no surprise lazy loads).
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def dispose(self) -> None:
        await self.engine.dispose()


__all__ = ["ApiKey", "Base", "Database", "Tenant", "TokenUsage"]
