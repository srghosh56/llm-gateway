"""Model registry: lifecycle state machine and audit logging.

    submitted --approve--> approved --deploy--> deployed --retire--> retired
        |                     |                                        ^
        +-------retire--------+----------------retire------------------+

The allowed transitions live in one table (``TRANSITIONS``) so the rules are easy to read,
test exhaustively and change. Every state change writes an audit row *in the same database
transaction*, so a change can never be committed without its audit record (or vice versa).
"""

from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.auth import Principal
from gateway.db import AuditLog, ModelEntry


class Status(StrEnum):
    SUBMITTED = "submitted"
    APPROVED = "approved"
    DEPLOYED = "deployed"
    RETIRED = "retired"


# action name -> resulting status
ACTIONS: dict[str, Status] = {
    "approve": Status.APPROVED,
    "deploy": Status.DEPLOYED,
    "retire": Status.RETIRED,
}

# current status -> statuses it may move to
TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.SUBMITTED: frozenset({Status.APPROVED, Status.RETIRED}),
    Status.APPROVED: frozenset({Status.DEPLOYED, Status.RETIRED}),
    Status.DEPLOYED: frozenset({Status.RETIRED}),
    Status.RETIRED: frozenset(),  # terminal: re-submit under a new name instead
}


class InvalidTransition(Exception):
    pass


def check_transition(current: Status, target: Status) -> None:
    if target not in TRANSITIONS[current]:
        raise InvalidTransition(f"cannot move model from '{current}' to '{target}'")


def audit(
    session: AsyncSession,
    actor: Principal,
    action: str,
    target_type: str,
    target_id: object,
    **details: object,
) -> None:
    """Stage an audit row; it commits together with the caller's change."""
    session.add(
        AuditLog(
            actor=actor.subject,
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            details=details,
        )
    )


def transition(session: AsyncSession, model: ModelEntry, action: str, actor: Principal) -> None:
    target = ACTIONS[action]
    current = Status(model.status)
    check_transition(current, target)
    model.status = target
    audit(
        session,
        actor,
        f"model.{action}",
        "model",
        model.name,
        **{"from": str(current), "to": str(target)},
    )


async def get_deployed(session: AsyncSession, name: str) -> ModelEntry | None:
    return await session.scalar(
        select(ModelEntry).where(ModelEntry.name == name, ModelEntry.status == Status.DEPLOYED)
    )


async def list_deployed(session: AsyncSession) -> list[ModelEntry]:
    rows = await session.scalars(
        select(ModelEntry).where(ModelEntry.status == Status.DEPLOYED).order_by(ModelEntry.name)
    )
    return list(rows)
