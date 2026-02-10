"""Audit service — append-only event log for every significant action.

Every submission, review, decision, retraction, and sanction is recorded here.
Events are immutable once written. This is the system's source of truth for provenance.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from alexandria.database import to_json
from alexandria.models import AuditAction, AuditEvent


async def log_event(
    db: aiosqlite.Connection,
    action: AuditAction,
    actor_id: str = "",
    target_id: str = "",
    target_type: str = "",
    details: dict[str, Any] | None = None,
    signature: str = "",
) -> AuditEvent:
    """Write an immutable audit event."""
    event = AuditEvent(
        action=action,
        actor_id=actor_id,
        target_id=target_id,
        target_type=target_type,
        details=details or {},
        signature=signature,
    )
    await db.execute(
        """
        INSERT INTO audit_events (event_id, action, actor_id, target_id, target_type, details, signature, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.event_id,
            event.action.value,
            event.actor_id,
            event.target_id,
            event.target_type,
            to_json(event.details),
            event.signature,
            event.timestamp.isoformat(),
        ),
    )
    await db.commit()
    return event


async def get_events_for_target(
    db: aiosqlite.Connection,
    target_id: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Retrieve audit events for a given target (scroll, scholar, etc.)."""
    async with db.execute(
        """
        SELECT * FROM audit_events
        WHERE target_id = ?
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (target_id, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_events_by_actor(
    db: aiosqlite.Connection,
    actor_id: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Retrieve audit events by a given actor."""
    async with db.execute(
        """
        SELECT * FROM audit_events
        WHERE actor_id = ?
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (actor_id, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_recent_events(
    db: aiosqlite.Connection,
    action: AuditAction | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Retrieve recent audit events, optionally filtered by action type."""
    if action:
        query = "SELECT * FROM audit_events WHERE action = ? ORDER BY timestamp DESC LIMIT ?"
        params = (action.value, limit)
    else:
        query = "SELECT * FROM audit_events ORDER BY timestamp DESC LIMIT ?"
        params = (limit,)

    async with db.execute(query, params) as cursor:
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]
