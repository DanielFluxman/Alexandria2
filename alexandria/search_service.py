"""Search service — semantic search, related work discovery, trending topics, gap identification."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from alexandria.database import from_json, get_chroma_collection
from alexandria.models import ScrollStatus, SearchResult


# ---------------------------------------------------------------------------
# Semantic search
# ---------------------------------------------------------------------------

async def search_scrolls(
    db: aiosqlite.Connection,
    query: str,
    domain: str | None = None,
    scroll_type: str | None = None,
    published_only: bool = True,
    limit: int = 20,
) -> list[SearchResult]:
    """
    Semantic search across scrolls using ChromaDB vector similarity.
    Returns results ranked by relevance.
    """
    try:
        collection = get_chroma_collection()

        where_filter: dict[str, Any] = {}
        conditions = []
        if published_only:
            conditions.append({"status": "published"})
        if domain:
            conditions.append({"domain": domain})
        if scroll_type:
            conditions.append({"scroll_type": scroll_type})

        if len(conditions) > 1:
            where_filter = {"$and": conditions}
        elif len(conditions) == 1:
            where_filter = conditions[0]

        results = collection.query(
            query_texts=[query],
            n_results=limit,
            where=where_filter if where_filter else None,
        )

        if not results or not results["ids"] or not results["ids"][0]:
            return []

        # Fetch full scroll data from SQLite for the matched IDs
        search_results: list[SearchResult] = []
        for i, scroll_id in enumerate(results["ids"][0]):
            distance = results["distances"][0][i] if results["distances"] else 0
            relevance = 1.0 - (distance / 2.0)  # Convert cosine distance to similarity

            async with db.execute(
                "SELECT scroll_id, title, abstract, domain, authors, citation_count, status, published_at FROM scrolls WHERE scroll_id = ?",
                (scroll_id,),
            ) as cursor:
                row = await cursor.fetchone()

            if row:
                search_results.append(SearchResult(
                    scroll_id=row[0],
                    title=row[1],
                    abstract=row[2],
                    domain=row[3],
                    authors=from_json(row[4]),
                    citation_count=row[5],
                    status=ScrollStatus(row[6]),
                    relevance_score=round(relevance, 4),
                    published_at=row[7],
                ))

        return search_results

    except Exception:
        # Fall back to SQLite full-text search
        return await _fallback_search(db, query, limit)


async def _fallback_search(
    db: aiosqlite.Connection,
    query: str,
    limit: int = 20,
) -> list[SearchResult]:
    """Simple LIKE-based fallback when ChromaDB is unavailable."""
    pattern = f"%{query}%"
    async with db.execute(
        """
        SELECT scroll_id, title, abstract, domain, authors, citation_count, status, published_at
        FROM scrolls
        WHERE status = 'published' AND (title LIKE ? OR abstract LIKE ? OR content LIKE ?)
        ORDER BY citation_count DESC
        LIMIT ?
        """,
        (pattern, pattern, pattern, limit),
    ) as cursor:
        rows = await cursor.fetchall()

    return [
        SearchResult(
            scroll_id=row[0],
            title=row[1],
            abstract=row[2],
            domain=row[3],
            authors=from_json(row[4]),
            citation_count=row[5],
            status=ScrollStatus(row[6]),
            relevance_score=0.5,
            published_at=row[7],
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Related work discovery
# ---------------------------------------------------------------------------

async def find_related(
    db: aiosqlite.Connection,
    scroll_id: str,
    limit: int = 10,
) -> list[SearchResult]:
    """Find semantically related scrolls to a given scroll."""
    # Get the scroll's text for query
    async with db.execute(
        "SELECT title, abstract, content FROM scrolls WHERE scroll_id = ?",
        (scroll_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return []

    query_text = f"{row[0]} {row[1]}"

    results = await search_scrolls(db, query_text, limit=limit + 1)
    # Filter out the scroll itself
    return [r for r in results if r.scroll_id != scroll_id][:limit]


# ---------------------------------------------------------------------------
# Trending topics
# ---------------------------------------------------------------------------

async def get_trending_topics(
    db: aiosqlite.Connection,
    days: int = 30,
    limit: int = 15,
) -> list[dict[str, Any]]:
    """
    Identify trending topics based on recent publication and citation activity.

    Analyzes keywords from recently published/cited scrolls.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # Keywords from recently published scrolls
    async with db.execute(
        "SELECT keywords FROM scrolls WHERE published_at > ? AND status = 'published'",
        (cutoff,),
    ) as cursor:
        rows = await cursor.fetchall()

    keyword_counter: Counter[str] = Counter()
    for row in rows:
        kws = from_json(row[0])
        if isinstance(kws, list):
            keyword_counter.update(kws)

    # Keywords from recently cited scrolls (scrolls that received citations recently)
    async with db.execute(
        """
        SELECT s.keywords FROM scrolls s
        JOIN citations c ON s.scroll_id = c.cited_scroll_id
        WHERE c.created_at > ?
        """,
        (cutoff,),
    ) as cursor:
        rows = await cursor.fetchall()

    for row in rows:
        kws = from_json(row[0])
        if isinstance(kws, list):
            keyword_counter.update(kws)

    # Also count by domain
    async with db.execute(
        "SELECT domain, COUNT(*) as cnt FROM scrolls WHERE published_at > ? AND status = 'published' GROUP BY domain ORDER BY cnt DESC",
        (cutoff,),
    ) as cursor:
        domain_rows = await cursor.fetchall()

    trending = [
        {"keyword": kw, "activity_count": count}
        for kw, count in keyword_counter.most_common(limit)
    ]

    return trending


# ---------------------------------------------------------------------------
# Research gap identification
# ---------------------------------------------------------------------------

async def find_gaps(
    db: aiosqlite.Connection,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """
    Identify under-researched areas in the library.

    Gaps are domains/keywords with:
    - Few scrolls relative to citation demand
    - Open hypotheses lacking evidence
    - Keywords mentioned in scrolls but with no dedicated papers
    """
    gaps: list[dict[str, Any]] = []

    # 1. Domains with few published scrolls but scrolls in review (demand > supply)
    async with db.execute(
        """
        SELECT domain,
               SUM(CASE WHEN status = 'published' THEN 1 ELSE 0 END) as published,
               SUM(CASE WHEN status IN ('under_review', 'submitted', 'screened') THEN 1 ELSE 0 END) as in_pipeline,
               COUNT(*) as total
        FROM scrolls
        WHERE domain != ''
        GROUP BY domain
        HAVING published < 3
        ORDER BY in_pipeline DESC
        LIMIT ?
        """,
        (limit,),
    ) as cursor:
        rows = await cursor.fetchall()

    for row in rows:
        gaps.append({
            "type": "under_published_domain",
            "domain": row[0],
            "published_count": row[1],
            "in_pipeline_count": row[2],
            "suggestion": f"Domain '{row[0]}' has only {row[1]} published scroll(s) — needs more research",
        })

    # 2. Hypotheses without any supporting evidence or rebuttals
    async with db.execute(
        """
        SELECT s.scroll_id, s.title, s.domain
        FROM scrolls s
        LEFT JOIN citations c ON s.scroll_id = c.cited_scroll_id
        WHERE s.scroll_type = 'hypothesis' AND s.status = 'published'
        GROUP BY s.scroll_id
        HAVING COUNT(c.citing_scroll_id) = 0
        LIMIT ?
        """,
        (limit,),
    ) as cursor:
        rows = await cursor.fetchall()

    for row in rows:
        gaps.append({
            "type": "uncited_hypothesis",
            "scroll_id": row[0],
            "title": row[1],
            "domain": row[2],
            "suggestion": f"Hypothesis '{row[1]}' has no citations — needs investigation or rebuttal",
        })

    # 3. Scrolls in review queue with no reviewers yet
    async with db.execute(
        """
        SELECT s.scroll_id, s.title, s.domain
        FROM scrolls s
        LEFT JOIN reviews r ON s.scroll_id = r.scroll_id
        WHERE s.status = 'under_review'
        GROUP BY s.scroll_id
        HAVING COUNT(r.review_id) = 0
        ORDER BY s.created_at ASC
        LIMIT ?
        """,
        (limit,),
    ) as cursor:
        rows = await cursor.fetchall()

    for row in rows:
        gaps.append({
            "type": "needs_reviewers",
            "scroll_id": row[0],
            "title": row[1],
            "domain": row[2],
            "suggestion": f"Scroll '{row[1]}' is awaiting review — volunteers needed",
        })

    return gaps[:limit]
