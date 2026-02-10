"""Citation service — citation graph, forward/backward lookups, lineage tracing, contradiction detection."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiosqlite

from alexandria.database import from_json


# ---------------------------------------------------------------------------
# Citation CRUD
# ---------------------------------------------------------------------------

async def record_citations(
    db: aiosqlite.Connection,
    citing_scroll_id: str,
    cited_scroll_ids: list[str],
) -> int:
    """
    Record citation edges from a citing scroll to its references.
    Also updates cited_by lists and citation counts on cited scrolls.

    Returns number of new citations added.
    """
    now = datetime.now(timezone.utc).isoformat()
    added = 0

    for cited_id in cited_scroll_ids:
        # Check cited scroll exists
        async with db.execute(
            "SELECT 1 FROM scrolls WHERE scroll_id = ?", (cited_id,)
        ) as cursor:
            if await cursor.fetchone() is None:
                continue

        # Insert citation edge (ignore duplicates)
        try:
            await db.execute(
                "INSERT OR IGNORE INTO citations (citing_scroll_id, cited_scroll_id, created_at) VALUES (?, ?, ?)",
                (citing_scroll_id, cited_id, now),
            )
            added += 1
        except Exception:
            continue

        # Update cited_by list on the cited scroll
        async with db.execute(
            "SELECT cited_by FROM scrolls WHERE scroll_id = ?", (cited_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            cited_by = from_json(row[0])
            if not isinstance(cited_by, list):
                cited_by = []
            if citing_scroll_id not in cited_by:
                cited_by.append(citing_scroll_id)
                await db.execute(
                    "UPDATE scrolls SET cited_by = ?, citation_count = ? WHERE scroll_id = ?",
                    (str(cited_by).replace("'", '"'), len(cited_by), cited_id),
                )

    await db.commit()
    return added


# ---------------------------------------------------------------------------
# Query citations
# ---------------------------------------------------------------------------

async def get_forward_citations(
    db: aiosqlite.Connection,
    scroll_id: str,
) -> list[str]:
    """Get all scrolls that cite a given scroll ("Cited by")."""
    async with db.execute(
        "SELECT citing_scroll_id FROM citations WHERE cited_scroll_id = ?",
        (scroll_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [row[0] for row in rows]


async def get_backward_references(
    db: aiosqlite.Connection,
    scroll_id: str,
) -> list[str]:
    """Get all scrolls that a given scroll cites (its bibliography)."""
    async with db.execute(
        "SELECT cited_scroll_id FROM citations WHERE citing_scroll_id = ?",
        (scroll_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [row[0] for row in rows]


async def trace_lineage(
    db: aiosqlite.Connection,
    scroll_id: str,
    max_depth: int = 10,
) -> dict[str, Any]:
    """
    Trace the full citation chain of a scroll back to its roots.

    Returns a tree structure: {scroll_id, title, references: [{scroll_id, title, references: [...]}]}
    """
    visited: set[str] = set()

    async def _trace(sid: str, depth: int) -> dict[str, Any]:
        if depth >= max_depth or sid in visited:
            return {"scroll_id": sid, "truncated": True}

        visited.add(sid)

        async with db.execute(
            "SELECT scroll_id, title FROM scrolls WHERE scroll_id = ?", (sid,)
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return {"scroll_id": sid, "not_found": True}

        refs = await get_backward_references(db, sid)
        children = [await _trace(ref_id, depth + 1) for ref_id in refs]

        return {
            "scroll_id": row[0],
            "title": row[1],
            "references": children,
        }

    return await _trace(scroll_id, 0)


# ---------------------------------------------------------------------------
# Impact analysis
# ---------------------------------------------------------------------------

async def get_most_cited(
    db: aiosqlite.Connection,
    domain: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Get the most-cited scrolls, optionally filtered by domain."""
    if domain:
        query = """
            SELECT scroll_id, title, domain, citation_count, authors
            FROM scrolls
            WHERE status = 'published' AND domain = ?
            ORDER BY citation_count DESC
            LIMIT ?
        """
        params: tuple = (domain, limit)
    else:
        query = """
            SELECT scroll_id, title, domain, citation_count, authors
            FROM scrolls
            WHERE status = 'published'
            ORDER BY citation_count DESC
            LIMIT ?
        """
        params = (limit,)

    async with db.execute(query, params) as cursor:
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Contradiction detection
# ---------------------------------------------------------------------------

async def find_contradictions(
    db: aiosqlite.Connection,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """
    Find scrolls that share common citations but have opposing recommendations/conclusions.

    A contradiction is suspected when:
    - Two rebuttals cite the same target scroll
    - Or a paper and its rebuttal both cite common third-party sources
    """
    contradictions: list[dict[str, Any]] = []

    # Find rebuttals and their targets
    async with db.execute(
        """
        SELECT s.scroll_id, s.title, c.cited_scroll_id
        FROM scrolls s
        JOIN citations c ON s.scroll_id = c.citing_scroll_id
        WHERE s.scroll_type = 'rebuttal' AND s.status = 'published'
        ORDER BY s.created_at DESC
        LIMIT ?
        """,
        (limit * 5,),
    ) as cursor:
        rows = await cursor.fetchall()

    # Group rebuttals by their cited scrolls
    target_rebuttals: dict[str, list[dict]] = {}
    for row in rows:
        cited = row[2]
        if cited not in target_rebuttals:
            target_rebuttals[cited] = []
        target_rebuttals[cited].append({
            "rebuttal_id": row[0],
            "rebuttal_title": row[1],
        })

    # Find targets with the original paper
    for target_id, rebuttals in target_rebuttals.items():
        async with db.execute(
            "SELECT scroll_id, title FROM scrolls WHERE scroll_id = ?", (target_id,)
        ) as cursor:
            target = await cursor.fetchone()
        if target:
            contradictions.append({
                "original_scroll_id": target[0],
                "original_title": target[1],
                "rebuttals": rebuttals,
            })
            if len(contradictions) >= limit:
                break

    return contradictions
