"""Review service — peer review submission, scoring, reviewer assignment, conflict checks."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiosqlite

from alexandria.audit_service import log_event
from alexandria.database import from_json, to_json
from alexandria.models import (
    AuditAction,
    Review,
    ReviewRecommendation,
    ReviewScores,
    ReviewSubmission,
    ScrollStatus,
    SuggestedEdit,
)


# ---------------------------------------------------------------------------
# Row conversion
# ---------------------------------------------------------------------------

def _row_to_review(row: aiosqlite.Row | dict[str, Any]) -> Review:
    d = dict(row)
    scores_raw = from_json(d.get("scores", "{}"))
    d["scores"] = ReviewScores(**scores_raw) if isinstance(scores_raw, dict) else scores_raw
    d["suggested_edits"] = [
        SuggestedEdit(**e) if isinstance(e, dict) else e
        for e in from_json(d.get("suggested_edits", "[]"))
    ]
    d["recommendation"] = ReviewRecommendation(d["recommendation"])
    return Review(**d)


# ---------------------------------------------------------------------------
# Conflict of interest checks
# ---------------------------------------------------------------------------

async def check_conflicts(
    db: aiosqlite.Connection,
    scroll_id: str,
    reviewer_id: str,
    review_round: int | None = None,
) -> list[str]:
    """
    Check for conflicts of interest between reviewer and scroll.
    Returns list of conflict reasons (empty = no conflicts).
    """
    conflicts: list[str] = []

    # 1. Reviewer cannot review their own scroll
    async with db.execute(
        "SELECT authors FROM scrolls WHERE scroll_id = ?", (scroll_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if row:
        authors = from_json(row[0])
        if reviewer_id in authors:
            conflicts.append("reviewer_is_author")

    # 2. Check excessive reciprocal reviews (reviewer reviewed author's work > 3 times recently)
    if row:
        authors = from_json(row[0])
        for author_id in authors:
            async with db.execute(
                """
                SELECT COUNT(*) FROM reviews r
                JOIN scrolls s ON r.scroll_id = s.scroll_id
                WHERE r.reviewer_id = ? AND s.authors LIKE ?
                """,
                (reviewer_id, f'%"{author_id}"%'),
            ) as cursor:
                count = (await cursor.fetchone())[0]
            if count >= 3:
                conflicts.append(f"excessive_reciprocal_reviews_with_{author_id}")

    # 3. Check if reviewer already reviewed this scroll in this round
    if review_round is not None:
        async with db.execute(
            """
            SELECT COUNT(*) FROM reviews
            WHERE scroll_id = ? AND reviewer_id = ? AND review_round = ?
            """,
            (scroll_id, reviewer_id, review_round),
        ) as cursor:
            existing = (await cursor.fetchone())[0]
        if existing > 0:
            conflicts.append("already_reviewed_this_scroll_round")

    return conflicts


# ---------------------------------------------------------------------------
# Submit review
# ---------------------------------------------------------------------------

async def submit_review(
    db: aiosqlite.Connection,
    reviewer_id: str,
    submission: ReviewSubmission,
) -> tuple[Review | None, list[str]]:
    """
    Submit a peer review for a scroll.

    Returns (review, errors). Errors is non-empty if conflicts found.
    """
    # Check scroll exists and is under review
    async with db.execute(
        "SELECT status FROM scrolls WHERE scroll_id = ?", (submission.scroll_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None, ["scroll_not_found"]
    if row[0] != ScrollStatus.UNDER_REVIEW.value:
        return None, [f"scroll_status_is_{row[0]}_not_under_review"]

    # Determine review round
    async with db.execute(
        "SELECT COALESCE(MAX(review_round), 0) FROM reviews WHERE scroll_id = ?",
        (submission.scroll_id,),
    ) as cursor:
        max_round = (await cursor.fetchone())[0]

    # Check if there's a new revision since last review round
    async with db.execute(
        "SELECT version FROM scrolls WHERE scroll_id = ?", (submission.scroll_id,)
    ) as cursor:
        version = (await cursor.fetchone())[0]

    current_round = max(max_round, version)

    # Reviewer must exist
    async with db.execute(
        "SELECT COUNT(*) FROM scholars WHERE scholar_id = ?",
        (reviewer_id,),
    ) as cursor:
        reviewer_exists = (await cursor.fetchone())[0] > 0
    if not reviewer_exists:
        return None, ["reviewer_not_found"]

    # Block suspended reviewers
    async with db.execute(
        """
        SELECT COUNT(*) FROM sanctions
        WHERE scholar_id = ?
          AND sanction_type = 'review_suspension'
          AND (expires_at IS NULL OR expires_at > ?)
        """,
        (reviewer_id, datetime.now(timezone.utc).isoformat()),
    ) as cursor:
        suspended = (await cursor.fetchone())[0] > 0
    if suspended:
        return None, ["reviewer_suspended"]

    # Conflict checks
    conflicts = await check_conflicts(
        db,
        submission.scroll_id,
        reviewer_id,
        review_round=current_round,
    )
    if conflicts:
        return None, conflicts

    review = Review(
        scroll_id=submission.scroll_id,
        reviewer_id=reviewer_id,
        review_round=current_round,
        scores=submission.scores,
        recommendation=submission.recommendation,
        comments_to_authors=submission.comments_to_authors,
        suggested_edits=submission.suggested_edits,
        confidential_comments=submission.confidential_comments,
        reviewer_confidence=submission.reviewer_confidence,
    )

    await db.execute(
        """
        INSERT INTO reviews (
            review_id, scroll_id, reviewer_id, review_round,
            scores, recommendation, comments_to_authors,
            suggested_edits, confidential_comments, reviewer_confidence,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            review.review_id,
            review.scroll_id,
            review.reviewer_id,
            review.review_round,
            to_json(review.scores.model_dump()),
            review.recommendation.value,
            review.comments_to_authors,
            to_json([e.model_dump() for e in review.suggested_edits]),
            review.confidential_comments,
            review.reviewer_confidence,
            review.created_at.isoformat(),
        ),
    )
    await db.commit()

    await log_event(
        db,
        AuditAction.REVIEW_SUBMITTED,
        actor_id=reviewer_id,
        target_id=submission.scroll_id,
        target_type="scroll",
        details={
            "review_id": review.review_id,
            "round": review.review_round,
            "recommendation": review.recommendation.value,
            "overall_score": review.scores.overall,
        },
    )

    return review, []


# ---------------------------------------------------------------------------
# Query reviews
# ---------------------------------------------------------------------------

async def get_reviews_for_scroll(
    db: aiosqlite.Connection,
    scroll_id: str,
    review_round: int | None = None,
) -> list[Review]:
    """Get all reviews for a scroll, optionally filtered by round."""
    if review_round is not None:
        query = "SELECT * FROM reviews WHERE scroll_id = ? AND review_round = ? ORDER BY created_at"
        params = (scroll_id, review_round)
    else:
        query = "SELECT * FROM reviews WHERE scroll_id = ? ORDER BY review_round, created_at"
        params = (scroll_id,)

    async with db.execute(query, params) as cursor:
        rows = await cursor.fetchall()
    return [_row_to_review(row) for row in rows]


async def get_review_queue(
    db: aiosqlite.Connection,
    domain: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Get scrolls awaiting peer review, with current review counts."""
    if domain:
        query = """
            SELECT s.scroll_id, s.title, s.abstract, s.domain, s.scroll_type, s.authors, s.created_at,
                   COUNT(r.review_id) as review_count
            FROM scrolls s
            LEFT JOIN reviews r ON s.scroll_id = r.scroll_id
            WHERE s.status = 'under_review' AND s.domain = ?
            GROUP BY s.scroll_id
            ORDER BY review_count ASC, s.created_at ASC
            LIMIT ?
        """
        params = (domain, limit)
    else:
        query = """
            SELECT s.scroll_id, s.title, s.abstract, s.domain, s.scroll_type, s.authors, s.created_at,
                   COUNT(r.review_id) as review_count
            FROM scrolls s
            LEFT JOIN reviews r ON s.scroll_id = r.scroll_id
            WHERE s.status = 'under_review'
            GROUP BY s.scroll_id
            ORDER BY review_count ASC, s.created_at ASC
            LIMIT ?
        """
        params = (limit,)

    async with db.execute(query, params) as cursor:
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]
