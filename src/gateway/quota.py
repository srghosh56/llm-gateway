"""Monthly token quota.

Checked before a request (reject if the month's usage already reached the quota) and
recorded after it (actual tokens reported by the backend). That makes it a *soft* quota:
requests already in flight when the limit is reached may overshoot it slightly. A hard quota
would need token reservation up front, which is not worth the complexity here.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.db import TokenUsage


def current_month() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


async def used_tokens(session: AsyncSession, tenant_id: int, month: str | None = None) -> int:
    stmt = select(TokenUsage.tokens).where(
        TokenUsage.tenant_id == tenant_id, TokenUsage.month == (month or current_month())
    )
    return (await session.scalar(stmt)) or 0


async def record_usage(session: AsyncSession, tenant_id: int, tokens: int) -> None:
    if tokens <= 0:
        return
    # INSERT ... ON CONFLICT DO UPDATE: atomic increment, safe under concurrent requests.
    stmt = insert(TokenUsage).values(tenant_id=tenant_id, month=current_month(), tokens=tokens)
    stmt = stmt.on_conflict_do_update(
        index_elements=[TokenUsage.tenant_id, TokenUsage.month],
        set_={"tokens": TokenUsage.tokens + stmt.excluded.tokens},
    )
    await session.execute(stmt)
    await session.commit()
