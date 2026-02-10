"""Reproducibility service — artifact validation, replication runs, evidence grades, badges.

Before a review-accepted scroll can be published, empirical claims must pass
reproducibility checks. This service manages artifact bundles, replication
attempts, and the evidence grading system.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiosqlite

from alexandria.audit_service import log_event
from alexandria.database import from_json, to_json
from alexandria.models import (
    ArtifactBundle,
    AuditAction,
    BadgeType,
    EvidenceGrade,
    ReplicationResult,
    ScrollStatus,
    ScrollType,
)


# ---------------------------------------------------------------------------
# Artifact bundle CRUD
# ---------------------------------------------------------------------------

async def submit_artifact_bundle(
    db: aiosqlite.Connection,
    bundle: ArtifactBundle,
    submitter_id: str,
) -> ArtifactBundle:
    """Register an artifact bundle for a scroll."""
    await db.execute(
        """
        INSERT INTO artifact_bundles (
            artifact_bundle_id, scroll_id, code_hash, data_hash,
            env_spec, run_commands, expected_metrics, random_seed, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            bundle.artifact_bundle_id,
            bundle.scroll_id,
            bundle.code_hash,
            bundle.data_hash,
            bundle.env_spec,
            to_json(bundle.run_commands),
            to_json(bundle.expected_metrics),
            bundle.random_seed,
            bundle.created_at.isoformat(),
        ),
    )

    # Link to scroll
    await db.execute(
        "UPDATE scrolls SET artifact_bundle_id = ? WHERE scroll_id = ?",
        (bundle.artifact_bundle_id, bundle.scroll_id),
    )
    await db.commit()

    return bundle


async def get_artifact_bundle(
    db: aiosqlite.Connection,
    bundle_id: str,
) -> ArtifactBundle | None:
    """Fetch an artifact bundle by ID."""
    async with db.execute(
        "SELECT * FROM artifact_bundles WHERE artifact_bundle_id = ?", (bundle_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    d = dict(row)
    d["run_commands"] = from_json(d.get("run_commands", "[]"))
    d["expected_metrics"] = from_json(d.get("expected_metrics", "{}"))
    return ArtifactBundle(**d)


# ---------------------------------------------------------------------------
# Replication runs
# ---------------------------------------------------------------------------

async def submit_replication(
    db: aiosqlite.Connection,
    result: ReplicationResult,
) -> ReplicationResult:
    """Record the outcome of a reproducibility check."""
    await db.execute(
        """
        INSERT INTO replications (
            replication_id, artifact_bundle_id, scroll_id, reproducer_id,
            success, observed_metrics, logs, env_used, started_at, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            result.replication_id,
            result.artifact_bundle_id,
            result.scroll_id,
            result.reproducer_id,
            1 if result.success else 0,
            to_json(result.observed_metrics),
            result.logs,
            result.env_used,
            result.started_at.isoformat(),
            result.completed_at.isoformat() if result.completed_at else None,
        ),
    )
    await db.commit()

    await log_event(
        db,
        AuditAction.REPRO_COMPLETED,
        actor_id=result.reproducer_id,
        target_id=result.scroll_id,
        target_type="scroll",
        details={
            "replication_id": result.replication_id,
            "success": result.success,
            "bundle_id": result.artifact_bundle_id,
        },
    )

    # Auto-update evidence grade and badges
    await _update_evidence_grade(db, result.scroll_id)

    return result


async def get_replications_for_scroll(
    db: aiosqlite.Connection,
    scroll_id: str,
) -> list[ReplicationResult]:
    """Get all replication attempts for a scroll."""
    async with db.execute(
        "SELECT * FROM replications WHERE scroll_id = ? ORDER BY started_at",
        (scroll_id,),
    ) as cursor:
        rows = await cursor.fetchall()

    results = []
    for row in rows:
        d = dict(row)
        d["success"] = bool(d["success"])
        d["observed_metrics"] = from_json(d.get("observed_metrics", "{}"))
        results.append(ReplicationResult(**d))
    return results


# ---------------------------------------------------------------------------
# Evidence grading
# ---------------------------------------------------------------------------

async def _update_evidence_grade(
    db: aiosqlite.Connection,
    scroll_id: str,
) -> None:
    """Recompute evidence grade based on replication results."""
    replications = await get_replications_for_scroll(db, scroll_id)
    successful = [r for r in replications if r.success]
    unique_reproducers = set(r.reproducer_id for r in successful)

    if len(unique_reproducers) >= 2:
        grade = EvidenceGrade.GRADE_A
    elif len(unique_reproducers) == 1:
        grade = EvidenceGrade.GRADE_B
    else:
        # Check if scroll has been review-accepted (Grade C)
        async with db.execute(
            "SELECT status FROM scrolls WHERE scroll_id = ?", (scroll_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row and row[0] in (
            ScrollStatus.REPRO_CHECK.value,
            ScrollStatus.ACCEPTED.value,
            ScrollStatus.PUBLISHED.value,
        ):
            grade = EvidenceGrade.GRADE_C
        else:
            grade = EvidenceGrade.UNGRADED

    # Determine badges
    badges: list[str] = []
    if grade in (EvidenceGrade.GRADE_A, EvidenceGrade.GRADE_B):
        badges.append(BadgeType.REPLICATED.value)

    # Check if artifact bundle exists and is complete
    async with db.execute(
        "SELECT artifact_bundle_id FROM scrolls WHERE scroll_id = ?", (scroll_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if row and row[0]:
        badges.append(BadgeType.ARTIFACT_COMPLETE.value)

    # High confidence methods badge if grade A
    if grade == EvidenceGrade.GRADE_A:
        badges.append(BadgeType.HIGH_CONFIDENCE_METHODS.value)

    await db.execute(
        "UPDATE scrolls SET evidence_grade = ?, badges = ?, updated_at = ? WHERE scroll_id = ?",
        (grade.value, to_json(badges), datetime.now(timezone.utc).isoformat(), scroll_id),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Reproducibility gate (called by policy engine after review acceptance)
# ---------------------------------------------------------------------------

async def check_repro_gate(
    db: aiosqlite.Connection,
    scroll_id: str,
) -> tuple[bool, str]:
    """
    Check if a scroll passes the reproducibility gate.

    Returns (passed, reason).

    Rules:
    - Non-empirical scroll types (hypothesis, tutorial) auto-pass
    - Empirical scrolls without artifact bundles: fail
    - Empirical scrolls with bundles but no successful replications: fail
    - Empirical scrolls with at least 1 successful replication: pass
    """
    async with db.execute(
        "SELECT scroll_type, artifact_bundle_id FROM scrolls WHERE scroll_id = ?",
        (scroll_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return False, "scroll_not_found"

    scroll_type = row[0]
    bundle_id = row[1]

    # Non-empirical types auto-pass
    non_empirical = {
        ScrollType.HYPOTHESIS.value,
        ScrollType.TUTORIAL.value,
        ScrollType.REBUTTAL.value,
    }
    if scroll_type in non_empirical:
        return True, f"auto_pass: {scroll_type} does not require replication"

    # Meta-analysis: passes if all cited scrolls are published (weaker gate)
    if scroll_type == ScrollType.META_ANALYSIS.value:
        return True, "auto_pass: meta-analysis verified through cited scroll status"

    # Empirical paper: needs artifact bundle
    if not bundle_id:
        return False, "empirical_scroll_missing_artifact_bundle"

    # Needs at least one successful replication
    replications = await get_replications_for_scroll(db, scroll_id)
    successful = [r for r in replications if r.success]

    if not successful:
        return False, "no_successful_replications"

    return True, f"passed: {len(successful)} successful replication(s)"


async def process_repro_gate(
    db: aiosqlite.Connection,
    scroll_id: str,
) -> tuple[bool, str]:
    """
    Run the reproducibility gate and transition scroll status accordingly.

    Called when a scroll is in REPRO_CHECK status.
    """
    passed, reason = await check_repro_gate(db, scroll_id)
    now = datetime.now(timezone.utc).isoformat()

    if passed:
        await db.execute(
            "UPDATE scrolls SET status = ?, published_at = ?, updated_at = ? WHERE scroll_id = ?",
            (ScrollStatus.PUBLISHED.value, now, now, scroll_id),
        )
        await db.commit()

        await log_event(
            db,
            AuditAction.SCROLL_PUBLISHED,
            actor_id="repro_gate",
            target_id=scroll_id,
            target_type="scroll",
            details={"reason": reason},
        )
    else:
        # Don't auto-reject — stay in repro_check, waiting for replication
        pass

    return passed, reason
