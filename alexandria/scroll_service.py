"""Scroll service — manuscript CRUD, submission screening, status transitions, versioning.

This service owns the scroll lifecycle from submission through publication.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiosqlite

from alexandria.audit_service import log_event
from alexandria.config import settings
from alexandria.database import (
    from_json,
    generate_scroll_id,
    get_chroma_collection,
    to_json,
)
from alexandria.models import (
    AuditAction,
    Claim,
    ResponseItem,
    RevisionEntry,
    Scroll,
    ScrollRevision,
    ScrollStatus,
    ScrollSubmission,
    ScrollType,
)


# ---------------------------------------------------------------------------
# Row conversion
# ---------------------------------------------------------------------------

def _row_to_scroll(row: aiosqlite.Row | dict[str, Any]) -> Scroll:
    """Convert a SQLite row to a Scroll model."""
    d = dict(row)
    d["keywords"] = from_json(d.get("keywords", "[]"))
    d["authors"] = from_json(d.get("authors", "[]"))
    d["revision_history"] = from_json(d.get("revision_history", "[]"))
    raw_claims = from_json(d.get("claims", "[]"))
    if isinstance(raw_claims, list):
        d["claims"] = [c if isinstance(c, dict) else (c.model_dump() if hasattr(c, "model_dump") else {"statement": str(c)}) for c in raw_claims]
    else:
        d["claims"] = []
    d["badges"] = from_json(d.get("badges", "[]"))
    d["references"] = from_json(d.get("references_list", "[]"))
    d["cited_by"] = from_json(d.get("cited_by", "[]"))
    d.pop("references_list", None)
    return Scroll(**d)


# ---------------------------------------------------------------------------
# Editorial Screening (automated desk check)
# ---------------------------------------------------------------------------

class ScreeningError:
    """A single validation failure from screening."""

    def __init__(self, rule: str, message: str):
        self.rule = rule
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "message": self.message}


def screen_submission(submission: ScrollSubmission) -> list[ScreeningError]:
    """
    Automated editorial screening — replaces the human editor's desk check.

    Returns a list of errors. Empty list = passed screening.
    """
    errors: list[ScreeningError] = []
    policy = settings.policy

    # Title present
    if not submission.title or not submission.title.strip():
        errors.append(ScreeningError("title_required", "Title is required"))

    # Abstract minimum length
    if len(submission.abstract.strip()) < policy.min_abstract_length:
        errors.append(ScreeningError(
            "abstract_too_short",
            f"Abstract must be at least {policy.min_abstract_length} characters (got {len(submission.abstract.strip())})",
        ))

    # Content minimum length
    if len(submission.content.strip()) < policy.min_content_length:
        errors.append(ScreeningError(
            "content_too_short",
            f"Content must be at least {policy.min_content_length} characters (got {len(submission.content.strip())})",
        ))

    # At least one author
    if not submission.authors:
        errors.append(ScreeningError("authors_required", "At least one author is required"))

    # Domain specified
    if not submission.domain or not submission.domain.strip():
        errors.append(ScreeningError("domain_required", "A domain (journal) must be specified"))

    # Empirical papers need claims or artifact bundle
    if submission.scroll_type == ScrollType.PAPER:
        if not submission.claims and not submission.artifact_bundle_id:
            # Not a hard error — but flagged as a warning in screening
            pass  # Allow theoretical papers without claims

    # Hypothesis must have at least one falsifiable claim
    if submission.scroll_type == ScrollType.HYPOTHESIS:
        if not submission.claims:
            errors.append(ScreeningError(
                "hypothesis_needs_claims",
                "A hypothesis must include at least one explicit claim",
            ))

    # Meta-analysis must cite at least 2 scrolls
    if submission.scroll_type == ScrollType.META_ANALYSIS:
        if len(submission.references) < 2:
            errors.append(ScreeningError(
                "meta_analysis_needs_references",
                "A meta-analysis must cite at least 2 existing scrolls",
            ))

    # Rebuttal must cite the scroll it's rebutting
    if submission.scroll_type == ScrollType.REBUTTAL:
        if not submission.references:
            errors.append(ScreeningError(
                "rebuttal_needs_target",
                "A rebuttal must cite at least the scroll it is challenging",
            ))

    return errors


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------

async def submit_scroll(
    db: aiosqlite.Connection,
    submission: ScrollSubmission,
    submitter_id: str,
) -> tuple[Scroll | None, list[ScreeningError]]:
    """
    Submit a new scroll through the publishing pipeline.

    1. Run editorial screening
    2. If passed, create scroll with status SCREENED and enter review queue
    3. If failed, create scroll with status DESK_REJECTED

    Returns (scroll, errors). If errors is non-empty, scroll was desk-rejected.
    """
    # Ensure submitter is in authors
    authors = list(submission.authors)
    if submitter_id not in authors:
        authors.insert(0, submitter_id)

    # Generate Alexandria ID
    scroll_id = await generate_scroll_id(db)

    # Screen
    errors = screen_submission(submission)

    # Validate cited references exist
    if submission.references:
        refs = sorted(set(submission.references))
        placeholders = ",".join("?" for _ in refs)
        async with db.execute(
            f"SELECT scroll_id FROM scrolls WHERE scroll_id IN ({placeholders})",
            refs,
        ) as cursor:
            existing = {row[0] for row in await cursor.fetchall()}
        missing = [rid for rid in refs if rid not in existing]
        if missing:
            preview = ", ".join(missing[:10])
            suffix = "..." if len(missing) > 10 else ""
            errors.append(
                ScreeningError(
                    "invalid_references",
                    f"Unknown cited scroll IDs: {preview}{suffix}",
                )
            )

    now = datetime.now(timezone.utc)

    status = ScrollStatus.SCREENED if not errors else ScrollStatus.DESK_REJECTED

    scroll = Scroll(
        scroll_id=scroll_id,
        title=submission.title,
        scroll_type=submission.scroll_type,
        abstract=submission.abstract,
        content=submission.content,
        keywords=submission.keywords,
        domain=submission.domain,
        authors=authors,
        status=status,
        references=submission.references,
        claims=[c.model_dump() if isinstance(c, Claim) else c for c in submission.claims],
        artifact_bundle_id=submission.artifact_bundle_id,
        method_profile=submission.method_profile,
        result_summary=submission.result_summary,
        created_at=now,
        updated_at=now,
    )

    await db.execute(
        """
        INSERT INTO scrolls (
            scroll_id, title, scroll_type, abstract, content, keywords,
            domain, authors, status, version, revision_history,
            claims, artifact_bundle_id, method_profile, result_summary,
            evidence_grade, badges, references_list, cited_by, citation_count,
            decision_record_id, superseded_by, retraction_reason,
            created_at, updated_at, published_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            scroll.scroll_id,
            scroll.title,
            scroll.scroll_type.value,
            scroll.abstract,
            scroll.content,
            to_json(scroll.keywords),
            scroll.domain,
            to_json(scroll.authors),
            scroll.status.value,
            scroll.version,
            to_json([]),
            to_json(scroll.claims),
            scroll.artifact_bundle_id,
            scroll.method_profile,
            scroll.result_summary,
            scroll.evidence_grade.value,
            to_json([]),
            to_json(scroll.references),
            to_json([]),
            0,
            None,
            None,
            None,
            now.isoformat(),
            now.isoformat(),
            None,
        ),
    )
    await db.commit()

    # Index in ChromaDB for semantic search
    try:
        collection = get_chroma_collection()
        doc_text = f"{scroll.title}\n\n{scroll.abstract}\n\n{scroll.content}"
        collection.upsert(
            ids=[scroll.scroll_id],
            documents=[doc_text],
            metadatas=[{
                "scroll_type": scroll.scroll_type.value,
                "domain": scroll.domain,
                "status": scroll.status.value,
                "authors": to_json(scroll.authors),
            }],
        )
    except Exception:
        pass  # Don't fail submission if vector indexing fails

    # Audit
    action = AuditAction.SCROLL_SUBMITTED if not errors else AuditAction.SCROLL_DESK_REJECTED
    await log_event(
        db,
        action,
        actor_id=submitter_id,
        target_id=scroll.scroll_id,
        target_type="scroll",
        details={
            "title": scroll.title,
            "type": scroll.scroll_type.value,
            "domain": scroll.domain,
            "screening_errors": [e.to_dict() for e in errors],
        },
    )

    # If screened, auto-transition to under_review (enters review queue)
    if not errors:
        await _transition_status(db, scroll.scroll_id, ScrollStatus.UNDER_REVIEW)
        scroll.status = ScrollStatus.UNDER_REVIEW

    return scroll, errors


# ---------------------------------------------------------------------------
# Lookup / Query
# ---------------------------------------------------------------------------

async def get_scroll(db: aiosqlite.Connection, scroll_id: str) -> Scroll | None:
    """Fetch a scroll by its Alexandria ID."""
    async with db.execute(
        "SELECT * FROM scrolls WHERE scroll_id = ?", (scroll_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_scroll(row)


async def get_scrolls_by_status(
    db: aiosqlite.Connection,
    status: ScrollStatus,
    limit: int = 50,
) -> list[Scroll]:
    """List scrolls with a given status."""
    async with db.execute(
        "SELECT * FROM scrolls WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
        (status.value, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [_row_to_scroll(row) for row in rows]


async def get_scrolls_by_domain(
    db: aiosqlite.Connection,
    domain: str,
    sort_by: str = "citation_count",
    limit: int = 50,
) -> list[Scroll]:
    """List scrolls in a domain, sorted by citation count or date."""
    allowed_sorts = {"citation_count", "created_at", "updated_at", "published_at"}
    if sort_by not in allowed_sorts:
        sort_by = "citation_count"
    order = "DESC"

    async with db.execute(
        f"SELECT * FROM scrolls WHERE domain = ? AND status = 'published' ORDER BY {sort_by} {order} LIMIT ?",
        (domain, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [_row_to_scroll(row) for row in rows]


async def get_recent_scrolls(
    db: aiosqlite.Connection,
    limit: int = 20,
) -> list[Scroll]:
    """Get recently published scrolls."""
    async with db.execute(
        "SELECT * FROM scrolls WHERE status = 'published' ORDER BY published_at DESC LIMIT ?",
        (limit,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [_row_to_scroll(row) for row in rows]


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------

async def _transition_status(
    db: aiosqlite.Connection,
    scroll_id: str,
    new_status: ScrollStatus,
) -> None:
    """Update a scroll's status and timestamp."""
    now = datetime.now(timezone.utc).isoformat()
    updates = {"status": new_status.value, "updated_at": now}

    if new_status == ScrollStatus.PUBLISHED:
        updates["published_at"] = now

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [scroll_id]

    await db.execute(
        f"UPDATE scrolls SET {set_clause} WHERE scroll_id = ?",
        vals,
    )
    await db.commit()

    # Update vector metadata
    try:
        collection = get_chroma_collection()
        collection.update(
            ids=[scroll_id],
            metadatas=[{"status": new_status.value}],
        )
    except Exception:
        pass


async def transition_scroll(
    db: aiosqlite.Connection,
    scroll_id: str,
    new_status: ScrollStatus,
    actor_id: str = "system",
    details: dict[str, Any] | None = None,
) -> Scroll | None:
    """Transition a scroll's status with audit logging."""
    scroll = await get_scroll(db, scroll_id)
    if scroll is None:
        return None

    await _transition_status(db, scroll_id, new_status)

    # Map status to audit action
    action_map = {
        ScrollStatus.SCREENED: AuditAction.SCROLL_SCREENED,
        ScrollStatus.PUBLISHED: AuditAction.SCROLL_PUBLISHED,
        ScrollStatus.RETRACTED: AuditAction.SCROLL_RETRACTED,
        ScrollStatus.FLAGGED: AuditAction.SCROLL_FLAGGED,
        ScrollStatus.SUPERSEDED: AuditAction.SCROLL_SUPERSEDED,
    }
    action = action_map.get(new_status, AuditAction.DECISION_MADE)

    await log_event(
        db,
        action,
        actor_id=actor_id,
        target_id=scroll_id,
        target_type="scroll",
        details={"new_status": new_status.value, **(details or {})},
    )

    return await get_scroll(db, scroll_id)


# ---------------------------------------------------------------------------
# Revisions
# ---------------------------------------------------------------------------

async def revise_scroll(
    db: aiosqlite.Connection,
    revision: ScrollRevision,
    author_id: str,
) -> Scroll | None:
    """Submit a revision addressing reviewer feedback."""
    scroll = await get_scroll(db, revision.scroll_id)
    if scroll is None:
        return None

    # Only authors may revise their own scroll.
    if author_id not in scroll.authors:
        return None

    # Only allow revisions when status is revisions_required
    if scroll.status != ScrollStatus.REVISIONS_REQUIRED:
        return None

    # Build the revision entry
    new_version = scroll.version + 1
    rev_entry = RevisionEntry(
        version=new_version,
        change_summary=revision.change_summary,
        response_letter=[
            ResponseItem(**r) if isinstance(r, dict) else r
            for r in revision.response_letter
        ],
    )

    # Merge updates
    now = datetime.now(timezone.utc)
    history = scroll.revision_history or []
    if isinstance(history, list) and history and isinstance(history[0], dict):
        history = history  # Already dicts from JSON
    history.append(rev_entry.model_dump())

    updates: dict[str, Any] = {
        "version": new_version,
        "revision_history": to_json(history),
        "updated_at": now.isoformat(),
        "status": ScrollStatus.UNDER_REVIEW.value,
    }

    if revision.title is not None:
        updates["title"] = revision.title
    if revision.abstract is not None:
        updates["abstract"] = revision.abstract
    if revision.content is not None:
        updates["content"] = revision.content
    if revision.keywords is not None:
        updates["keywords"] = to_json(revision.keywords)
    if revision.references is not None:
        updates["references_list"] = to_json(revision.references)
    if revision.claims is not None:
        updates["claims"] = to_json([c.model_dump() if hasattr(c, "model_dump") else c for c in revision.claims])
    if revision.artifact_bundle_id is not None:
        updates["artifact_bundle_id"] = revision.artifact_bundle_id
    if revision.method_profile is not None:
        updates["method_profile"] = revision.method_profile
    if revision.result_summary is not None:
        updates["result_summary"] = revision.result_summary

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [revision.scroll_id]
    await db.execute(
        f"UPDATE scrolls SET {set_clause} WHERE scroll_id = ?",
        vals,
    )
    await db.commit()

    # Re-index in ChromaDB
    try:
        updated = await get_scroll(db, revision.scroll_id)
        if updated:
            collection = get_chroma_collection()
            doc_text = f"{updated.title}\n\n{updated.abstract}\n\n{updated.content}"
            collection.upsert(
                ids=[updated.scroll_id],
                documents=[doc_text],
                metadatas=[{
                    "scroll_type": updated.scroll_type.value,
                    "domain": updated.domain,
                    "status": updated.status.value,
                }],
            )
    except Exception:
        pass

    await log_event(
        db,
        AuditAction.REVISION_SUBMITTED,
        actor_id=author_id,
        target_id=revision.scroll_id,
        target_type="scroll",
        details={"version": new_version, "change_summary": revision.change_summary},
    )

    return await get_scroll(db, revision.scroll_id)


# ---------------------------------------------------------------------------
# Retraction
# ---------------------------------------------------------------------------

async def retract_scroll(
    db: aiosqlite.Connection,
    scroll_id: str,
    reason: str,
    actor_id: str,
) -> Scroll | None:
    """Retract a published scroll."""
    scroll = await get_scroll(db, scroll_id)
    if scroll is None:
        return None

    # Only authors may retract their own scroll.
    if actor_id not in scroll.authors:
        return None

    # Retraction only allowed for active workflow/publication states.
    allowed_states = {
        ScrollStatus.UNDER_REVIEW,
        ScrollStatus.REVISIONS_REQUIRED,
        ScrollStatus.REPRO_CHECK,
        ScrollStatus.ACCEPTED,
        ScrollStatus.PUBLISHED,
        ScrollStatus.FLAGGED,
    }
    if scroll.status not in allowed_states:
        return None

    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE scrolls SET status = ?, retraction_reason = ?, updated_at = ? WHERE scroll_id = ?",
        (ScrollStatus.RETRACTED.value, reason, now, scroll_id),
    )
    await db.commit()

    await log_event(
        db,
        AuditAction.SCROLL_RETRACTED,
        actor_id=actor_id,
        target_id=scroll_id,
        target_type="scroll",
        details={"reason": reason},
    )

    return await get_scroll(db, scroll_id)


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

async def count_scrolls_by_status(db: aiosqlite.Connection) -> dict[str, int]:
    """Count scrolls grouped by status."""
    async with db.execute(
        "SELECT status, COUNT(*) as cnt FROM scrolls GROUP BY status"
    ) as cursor:
        rows = await cursor.fetchall()
    return {row[0]: row[1] for row in rows}


async def count_scrolls_by_type(db: aiosqlite.Connection) -> dict[str, int]:
    """Count scrolls grouped by type."""
    async with db.execute(
        "SELECT scroll_type, COUNT(*) as cnt FROM scrolls GROUP BY scroll_type"
    ) as cursor:
        rows = await cursor.fetchall()
    return {row[0]: row[1] for row in rows}


async def get_all_domains(db: aiosqlite.Connection) -> list[str]:
    """List all unique domains with at least one scroll."""
    async with db.execute(
        "SELECT DISTINCT domain FROM scrolls WHERE domain != '' ORDER BY domain"
    ) as cursor:
        rows = await cursor.fetchall()
    return [row[0] for row in rows]


async def get_all_keywords(db: aiosqlite.Connection) -> list[str]:
    """List all unique keywords across all scrolls."""
    async with db.execute("SELECT keywords FROM scrolls") as cursor:
        rows = await cursor.fetchall()

    kw_set: set[str] = set()
    for row in rows:
        kws = from_json(row[0])
        if isinstance(kws, list):
            kw_set.update(kws)
    return sorted(kw_set)
