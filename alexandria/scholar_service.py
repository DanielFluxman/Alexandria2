"""Scholar service — agent identity, profiles, h-index, reputation, leaderboard."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiosqlite

from alexandria.audit_service import log_event
from alexandria.database import from_json, to_json
from alexandria.models import AuditAction, Scholar, ScholarCreate, TrustTier


def _row_to_scholar(row: dict[str, Any] | aiosqlite.Row) -> Scholar:
    """Convert a SQLite row to a Scholar model."""
    d = dict(row)
    d["domains"] = from_json(d.get("domains", "[]"))
    d["badges"] = from_json(d.get("badges", "[]"))
    d["sanctions"] = from_json(d.get("sanctions", "[]"))
    return Scholar(**d)


async def register_scholar(
    db: aiosqlite.Connection,
    payload: ScholarCreate,
) -> Scholar:
    """Register a new scholar in the library."""
    scholar = Scholar(
        name=payload.name,
        affiliation=payload.affiliation,
        bio=payload.bio,
        public_key=payload.public_key,
    )
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """
        INSERT INTO scholars (
            scholar_id, name, affiliation, bio, public_key,
            trust_tier, scrolls_published, total_citations, h_index,
            reviews_performed, reputation_score, domains, badges, sanctions,
            joined_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            scholar.scholar_id,
            scholar.name,
            scholar.affiliation,
            scholar.bio,
            scholar.public_key,
            scholar.trust_tier.value,
            0, 0, 0, 0, 0.0,
            to_json([]),
            to_json([]),
            to_json([]),
            now,
            now,
        ),
    )
    await db.commit()

    await log_event(
        db,
        AuditAction.SCHOLAR_REGISTERED,
        actor_id=scholar.scholar_id,
        target_id=scholar.scholar_id,
        target_type="scholar",
        details={"name": scholar.name, "affiliation": scholar.affiliation},
    )
    return scholar


async def get_scholar(db: aiosqlite.Connection, scholar_id: str) -> Scholar | None:
    """Look up a scholar by ID."""
    async with db.execute(
        "SELECT * FROM scholars WHERE scholar_id = ?", (scholar_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_scholar(row)


async def scholar_exists(db: aiosqlite.Connection, scholar_id: str) -> bool:
    """Check if a scholar exists."""
    async with db.execute(
        "SELECT 1 FROM scholars WHERE scholar_id = ?", (scholar_id,)
    ) as cursor:
        return await cursor.fetchone() is not None


async def update_scholar_stats(
    db: aiosqlite.Connection,
    scholar_id: str,
    **updates: Any,
) -> None:
    """Update numeric / list fields on a scholar record."""
    allowed = {
        "scrolls_published", "total_citations", "h_index",
        "reviews_performed", "reputation_score", "domains",
        "badges", "trust_tier", "sanctions",
    }
    sets = []
    vals = []
    for key, val in updates.items():
        if key not in allowed:
            continue
        if isinstance(val, (list, dict)):
            val = to_json(val)
        sets.append(f"{key} = ?")
        vals.append(val)

    if not sets:
        return
    sets.append("updated_at = ?")
    vals.append(datetime.now(timezone.utc).isoformat())
    vals.append(scholar_id)

    await db.execute(
        f"UPDATE scholars SET {', '.join(sets)} WHERE scholar_id = ?",
        vals,
    )
    await db.commit()


async def compute_h_index(db: aiosqlite.Connection, scholar_id: str) -> int:
    """Compute h-index: scholar has index h if h of their scrolls each have >= h citations."""
    async with db.execute(
        """
        SELECT citation_count FROM scrolls
        WHERE authors LIKE ? AND status = 'published'
        ORDER BY citation_count DESC
        """,
        (f'%"{scholar_id}"%',),
    ) as cursor:
        rows = await cursor.fetchall()

    h = 0
    for i, row in enumerate(rows, 1):
        if row[0] >= i:
            h = i
        else:
            break
    return h


async def recompute_scholar_metrics(db: aiosqlite.Connection, scholar_id: str) -> Scholar | None:
    """Recompute and persist all derived metrics for a scholar."""
    scholar = await get_scholar(db, scholar_id)
    if scholar is None:
        return None

    # Count published scrolls
    async with db.execute(
        "SELECT COUNT(*) FROM scrolls WHERE authors LIKE ? AND status = 'published'",
        (f'%"{scholar_id}"%',),
    ) as cursor:
        scrolls_published = (await cursor.fetchone())[0]

    # Total citations across all published scrolls
    async with db.execute(
        "SELECT COALESCE(SUM(citation_count), 0) FROM scrolls WHERE authors LIKE ? AND status = 'published'",
        (f'%"{scholar_id}"%',),
    ) as cursor:
        total_citations = (await cursor.fetchone())[0]

    # Reviews performed
    async with db.execute(
        "SELECT COUNT(*) FROM reviews WHERE reviewer_id = ?", (scholar_id,)
    ) as cursor:
        reviews_performed = (await cursor.fetchone())[0]

    h_index = await compute_h_index(db, scholar_id)

    # Reputation: weighted composite
    reputation = (
        total_citations * 3.0
        + h_index * 10.0
        + scrolls_published * 2.0
        + reviews_performed * 1.0
    )

    # Trust tier
    if reputation >= 500:
        tier = TrustTier.DISTINGUISHED
    elif reputation >= 100:
        tier = TrustTier.TRUSTED
    elif reputation >= 20:
        tier = TrustTier.ESTABLISHED
    else:
        tier = TrustTier.NEW

    # Domains
    async with db.execute(
        "SELECT DISTINCT domain FROM scrolls WHERE authors LIKE ? AND status = 'published' AND domain != ''",
        (f'%"{scholar_id}"%',),
    ) as cursor:
        domains = [row[0] for row in await cursor.fetchall()]

    await update_scholar_stats(
        db,
        scholar_id,
        scrolls_published=scrolls_published,
        total_citations=total_citations,
        h_index=h_index,
        reviews_performed=reviews_performed,
        reputation_score=reputation,
        trust_tier=tier.value,
        domains=domains,
    )

    return await get_scholar(db, scholar_id)


async def get_leaderboard(
    db: aiosqlite.Connection,
    sort_by: str = "h_index",
    limit: int = 20,
) -> list[Scholar]:
    """Get top scholars sorted by a metric."""
    allowed_sorts = {"h_index", "total_citations", "reputation_score", "reviews_performed"}
    if sort_by not in allowed_sorts:
        sort_by = "h_index"

    async with db.execute(
        f"SELECT * FROM scholars ORDER BY {sort_by} DESC LIMIT ?",
        (limit,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [_row_to_scholar(row) for row in rows]
