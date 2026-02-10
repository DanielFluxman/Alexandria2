"""Integrity service — anti-gaming controls, abuse detection, sanctions.

Detects:
- Sybil attacks (fake identities)
- Citation rings (mutual citation cartels)
- Plagiarism (near-duplicate content)
- Submission velocity anomalies

Applies automatic sanctions when violations are confirmed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from alexandria.audit_service import log_event
from alexandria.config import settings
from alexandria.database import from_json, get_chroma_collection, to_json
from alexandria.models import (
    AuditAction,
    BadgeType,
    Sanction,
    SanctionType,
)


# ---------------------------------------------------------------------------
# Plagiarism / similarity detection
# ---------------------------------------------------------------------------

async def check_plagiarism(
    db: aiosqlite.Connection,
    scroll_id: str,
    content: str,
) -> list[dict[str, Any]]:
    """
    Check if content is suspiciously similar to existing scrolls.

    Uses ChromaDB vector similarity. Returns list of matches above threshold.
    """
    threshold = settings.policy.plagiarism_similarity_threshold
    matches: list[dict[str, Any]] = []

    try:
        collection = get_chroma_collection()
        results = collection.query(
            query_texts=[content[:5000]],  # Use first 5k chars for comparison
            n_results=5,
            where={"status": {"$ne": "desk_rejected"}},
        )

        if results and results["ids"] and results["distances"]:
            for i, (doc_id, distance) in enumerate(
                zip(results["ids"][0], results["distances"][0])
            ):
                # ChromaDB cosine distance: 0 = identical, 2 = opposite
                similarity = 1.0 - (distance / 2.0)
                if doc_id != scroll_id and similarity >= threshold:
                    matches.append({
                        "matched_scroll_id": doc_id,
                        "similarity": round(similarity, 4),
                    })
    except Exception:
        pass  # Don't block on vector search failures

    return matches


# ---------------------------------------------------------------------------
# Citation ring detection
# ---------------------------------------------------------------------------

async def detect_citation_rings(
    db: aiosqlite.Connection,
    scholar_id: str,
) -> list[dict[str, Any]]:
    """
    Detect potential citation rings involving a scholar.

    A citation ring is when scholars excessively cite each other's work
    in a reciprocal pattern beyond normal academic practice.
    """
    threshold = settings.policy.citation_ring_threshold
    rings: list[dict[str, Any]] = []

    # Find scholars whose work this scholar cites
    async with db.execute(
        """
        SELECT c.cited_scroll_id, s.authors
        FROM citations c
        JOIN scrolls s ON c.cited_scroll_id = s.scroll_id
        JOIN scrolls s2 ON c.citing_scroll_id = s2.scroll_id
        WHERE s2.authors LIKE ?
        """,
        (f'%"{scholar_id}"%',),
    ) as cursor:
        rows = await cursor.fetchall()

    # Count citations per author
    citation_targets: dict[str, int] = {}
    for row in rows:
        authors = from_json(row[1])
        for author in authors:
            if author != scholar_id:
                citation_targets[author] = citation_targets.get(author, 0) + 1

    # Check for reciprocal citations
    for target_id, outgoing_count in citation_targets.items():
        async with db.execute(
            """
            SELECT COUNT(*)
            FROM citations c
            JOIN scrolls s_citing ON c.citing_scroll_id = s_citing.scroll_id
            JOIN scrolls s_cited ON c.cited_scroll_id = s_cited.scroll_id
            WHERE s_citing.authors LIKE ? AND s_cited.authors LIKE ?
            """,
            (f'%"{target_id}"%', f'%"{scholar_id}"%'),
        ) as cursor:
            incoming_count = (await cursor.fetchone())[0]

        reciprocal = min(outgoing_count, incoming_count)
        if reciprocal >= threshold:
            rings.append({
                "scholar_id": target_id,
                "outgoing_citations": outgoing_count,
                "incoming_citations": incoming_count,
                "reciprocal_count": reciprocal,
            })

    return rings


# ---------------------------------------------------------------------------
# Sybil detection (submission velocity anomalies)
# ---------------------------------------------------------------------------

async def check_sybil_velocity(
    db: aiosqlite.Connection,
    scholar_id: str,
) -> dict[str, Any]:
    """
    Check if a scholar's submission rate is anomalously high.

    Returns a dict with violation=True if rate exceeds threshold.
    """
    window_hours = settings.policy.sybil_velocity_window_hours
    max_submissions = settings.policy.sybil_max_submissions_per_window

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()

    async with db.execute(
        """
        SELECT COUNT(*) FROM scrolls
        WHERE authors LIKE ? AND created_at > ?
        """,
        (f'%"{scholar_id}"%', cutoff),
    ) as cursor:
        count = (await cursor.fetchone())[0]

    return {
        "scholar_id": scholar_id,
        "submissions_in_window": count,
        "window_hours": window_hours,
        "max_allowed": max_submissions,
        "violation": count > max_submissions,
    }


# ---------------------------------------------------------------------------
# Flag and sanction
# ---------------------------------------------------------------------------

async def flag_scroll(
    db: aiosqlite.Connection,
    scroll_id: str,
    reason: str,
    flagged_by: str = "integrity_agent",
) -> None:
    """Flag a scroll for integrity concerns."""
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE scrolls SET status = 'flagged', updated_at = ? WHERE scroll_id = ?",
        (now, scroll_id),
    )

    # Add integrity_flagged badge
    async with db.execute(
        "SELECT badges FROM scrolls WHERE scroll_id = ?", (scroll_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if row:
        badges = from_json(row[0])
        if BadgeType.INTEGRITY_FLAGGED.value not in badges:
            badges.append(BadgeType.INTEGRITY_FLAGGED.value)
            await db.execute(
                "UPDATE scrolls SET badges = ? WHERE scroll_id = ?",
                (to_json(badges), scroll_id),
            )

    await db.commit()

    await log_event(
        db,
        AuditAction.SCROLL_FLAGGED,
        actor_id=flagged_by,
        target_id=scroll_id,
        target_type="scroll",
        details={"reason": reason},
    )


async def apply_sanction(
    db: aiosqlite.Connection,
    scholar_id: str,
    sanction_type: SanctionType,
    reason: str,
    scroll_id: str | None = None,
    duration_hours: int | None = None,
) -> Sanction:
    """Apply an automatic sanction to a scholar."""
    now = datetime.now(timezone.utc)
    expires_at = (
        (now + timedelta(hours=duration_hours)) if duration_hours else None
    )

    sanction = Sanction(
        scholar_id=scholar_id,
        sanction_type=sanction_type,
        reason=reason,
        scroll_id=scroll_id,
        expires_at=expires_at,
        applied_at=now,
    )

    await db.execute(
        """
        INSERT INTO sanctions (
            sanction_id, scholar_id, sanction_type, reason,
            scroll_id, expires_at, applied_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sanction.sanction_id,
            sanction.scholar_id,
            sanction.sanction_type.value,
            sanction.reason,
            sanction.scroll_id,
            sanction.expires_at.isoformat() if sanction.expires_at else None,
            sanction.applied_at.isoformat(),
        ),
    )
    await db.commit()

    await log_event(
        db,
        AuditAction.SANCTION_APPLIED,
        actor_id="integrity_agent",
        target_id=scholar_id,
        target_type="scholar",
        details={
            "sanction_type": sanction_type.value,
            "reason": reason,
            "scroll_id": scroll_id,
            "duration_hours": duration_hours,
        },
    )

    return sanction


async def get_active_sanctions(
    db: aiosqlite.Connection,
    scholar_id: str,
) -> list[Sanction]:
    """Get all currently active sanctions for a scholar."""
    now = datetime.now(timezone.utc).isoformat()
    async with db.execute(
        """
        SELECT * FROM sanctions
        WHERE scholar_id = ? AND (expires_at IS NULL OR expires_at > ?)
        ORDER BY applied_at DESC
        """,
        (scholar_id, now),
    ) as cursor:
        rows = await cursor.fetchall()
    return [Sanction(**dict(row)) for row in rows]


async def is_sanctioned(
    db: aiosqlite.Connection,
    scholar_id: str,
    action: str,
) -> bool:
    """Check if a scholar is currently sanctioned from a specific action."""
    sanctions = await get_active_sanctions(db, scholar_id)
    action_blocks = {
        "submit": {SanctionType.SUBMISSION_SUSPENSION},
        "review": {SanctionType.REVIEW_SUSPENSION},
    }
    blocked_types = action_blocks.get(action, set())
    return any(s.sanction_type in blocked_types for s in sanctions)


async def get_integrity_flags(
    db: aiosqlite.Connection,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Get all currently flagged scrolls."""
    async with db.execute(
        "SELECT * FROM scrolls WHERE status = 'flagged' ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]
