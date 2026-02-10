"""Policy engine — deterministic, auditable decisions for the publishing pipeline.

Every accept/reject/revision decision goes through this engine.
All decisions produce a DecisionRecord that can be replayed and explained.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from alexandria.audit_service import log_event
from alexandria.config import settings
from alexandria.database import to_json
from alexandria.models import (
    AuditAction,
    DecisionRecord,
    PolicyRuleEvaluation,
    ReviewRecommendation,
    ScrollStatus,
)
from alexandria.review_service import get_reviews_for_scroll


# ---------------------------------------------------------------------------
# Policy rules (each is a pure function returning a PolicyRuleEvaluation)
# ---------------------------------------------------------------------------

def _rule_min_reviews(review_count: int, domain: str) -> PolicyRuleEvaluation:
    """Check minimum review count is met."""
    policy = settings.policy
    required = (
        policy.min_reviews_high_impact
        if domain in policy.high_impact_domains
        else policy.min_reviews_normal
    )
    passed = review_count >= required
    return PolicyRuleEvaluation(
        rule_name="minimum_reviews",
        input_data={"review_count": review_count, "required": required, "domain": domain},
        result=passed,
        explanation=f"{'Met' if passed else 'Not met'}: {review_count}/{required} reviews received",
    )


def _rule_score_threshold(avg_overall: float) -> PolicyRuleEvaluation:
    """Check average overall review score meets threshold."""
    threshold = settings.policy.accept_score_threshold
    passed = avg_overall >= threshold
    return PolicyRuleEvaluation(
        rule_name="score_threshold",
        input_data={"avg_overall": round(avg_overall, 2), "threshold": threshold},
        result=passed,
        explanation=f"{'Met' if passed else 'Not met'}: avg score {avg_overall:.1f} vs threshold {threshold}",
    )


def _rule_no_reject_majority(recommendations: list[str]) -> PolicyRuleEvaluation:
    """Check that the majority of reviewers did not recommend reject."""
    reject_count = sum(1 for r in recommendations if r == ReviewRecommendation.REJECT.value)
    total = len(recommendations)
    majority_reject = reject_count > total / 2 if total > 0 else False
    return PolicyRuleEvaluation(
        rule_name="no_reject_majority",
        input_data={"reject_count": reject_count, "total": total},
        result=not majority_reject,
        explanation=f"{'FAIL: majority reject' if majority_reject else 'OK'}: {reject_count}/{total} reject",
    )


def _rule_no_unresolved_critical_flags(reviews_data: list[dict]) -> PolicyRuleEvaluation:
    """Check for unresolved critical flags in confidential comments."""
    # A "critical flag" is a review with recommendation=reject and confidence >= 0.8
    critical_flags = [
        r for r in reviews_data
        if r.get("recommendation") == ReviewRecommendation.REJECT.value
        and r.get("reviewer_confidence", 0) >= 0.8
    ]
    passed = len(critical_flags) == 0
    return PolicyRuleEvaluation(
        rule_name="no_unresolved_critical_flags",
        input_data={"critical_flag_count": len(critical_flags)},
        result=passed,
        explanation=f"{'OK' if passed else f'FAIL: {len(critical_flags)} critical flags'}",
    )


def _rule_revision_limit(version: int) -> PolicyRuleEvaluation:
    """Check if max revision rounds exceeded."""
    max_rounds = settings.policy.max_revision_rounds
    within_limit = version <= max_rounds + 1  # version 1 = original, so +1
    return PolicyRuleEvaluation(
        rule_name="revision_limit",
        input_data={"current_version": version, "max_rounds": max_rounds},
        result=within_limit,
        explanation=f"{'OK' if within_limit else 'EXCEEDED'}: version {version}, max {max_rounds} revision rounds",
    )


def _rule_revisions_needed(recommendations: list[str]) -> PolicyRuleEvaluation:
    """Check if any reviewer requests revisions."""
    revision_recs = [
        r for r in recommendations
        if r in (ReviewRecommendation.MINOR_REVISIONS.value, ReviewRecommendation.MAJOR_REVISIONS.value)
    ]
    needs_revisions = len(revision_recs) > 0
    return PolicyRuleEvaluation(
        rule_name="revisions_needed",
        input_data={"revision_requests": len(revision_recs), "total": len(recommendations)},
        result=needs_revisions,
        explanation=f"{len(revision_recs)}/{len(recommendations)} reviewers request revisions",
    )


# ---------------------------------------------------------------------------
# Decision engine
# ---------------------------------------------------------------------------

async def evaluate_scroll(
    db: aiosqlite.Connection,
    scroll_id: str,
) -> DecisionRecord | None:
    """
    Run all policy rules against a scroll and produce a deterministic decision.

    Possible outcomes: accept, reject, revisions_required, insufficient_reviews.
    """
    # Gather data
    async with db.execute(
        "SELECT domain, version, status, scroll_type FROM scrolls WHERE scroll_id = ?",
        (scroll_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None

    domain = row[0]
    version = row[1]
    current_status = row[2]
    scroll_type = row[3]

    # Only evaluate scrolls that are under review
    if current_status != ScrollStatus.UNDER_REVIEW.value:
        return None

    reviews = await get_reviews_for_scroll(db, scroll_id)
    review_count = len(reviews)
    recommendations = [r.recommendation.value for r in reviews]
    avg_overall = (
        sum(r.scores.overall for r in reviews) / review_count
        if review_count > 0
        else 0.0
    )
    reviews_data = [
        {
            "recommendation": r.recommendation.value,
            "reviewer_confidence": r.reviewer_confidence,
            "overall_score": r.scores.overall,
        }
        for r in reviews
    ]

    # Run rules
    evals: list[PolicyRuleEvaluation] = []

    e_min = _rule_min_reviews(review_count, domain)
    evals.append(e_min)

    # If insufficient reviews, can't make a decision yet
    if not e_min.result:
        record = DecisionRecord(
            scroll_id=scroll_id,
            decision="insufficient_reviews",
            rule_evaluations=evals,
            review_summary={
                "review_count": review_count,
                "avg_overall": round(avg_overall, 2),
                "recommendations": recommendations,
            },
            explanation=f"Waiting for more reviews: {e_min.explanation}",
        )
        return record

    e_version = _rule_revision_limit(version)
    evals.append(e_version)

    e_score = _rule_score_threshold(avg_overall)
    evals.append(e_score)

    e_reject = _rule_no_reject_majority(recommendations)
    evals.append(e_reject)

    e_critical = _rule_no_unresolved_critical_flags(reviews_data)
    evals.append(e_critical)

    e_revisions = _rule_revisions_needed(recommendations)
    evals.append(e_revisions)

    # Determine decision
    review_summary = {
        "review_count": review_count,
        "avg_overall": round(avg_overall, 2),
        "recommendations": recommendations,
    }

    # Auto-reject if revision limit exceeded
    if not e_version.result:
        decision = "reject"
        explanation = "Max revision rounds exceeded — auto-rejected by policy"
    # Reject if majority recommend reject
    elif not e_reject.result:
        decision = "reject"
        explanation = "Majority of reviewers recommend rejection"
    # Reject if critical flags unresolved
    elif not e_critical.result:
        decision = "reject"
        explanation = "Unresolved critical flags from high-confidence reviewers"
    # Request revisions if any reviewer asks and score is borderline
    elif e_revisions.result and not e_score.result:
        decision = "revisions_required"
        explanation = "Reviewers request revisions and score is below threshold"
    elif e_revisions.result and e_score.result:
        # Score is good but some reviewers want revisions — minor revisions
        major_revisions = sum(1 for r in recommendations if r == ReviewRecommendation.MAJOR_REVISIONS.value)
        if major_revisions > 0:
            decision = "revisions_required"
            explanation = "Score meets threshold but major revisions requested"
        else:
            # Minor revisions with good score — accept (authors can address minor issues in final version)
            decision = "accept"
            explanation = "Score meets threshold; minor revision requests can be addressed post-acceptance"
    elif e_score.result:
        decision = "accept"
        explanation = "All criteria met: sufficient reviews, score above threshold, no critical flags"
    else:
        decision = "revisions_required"
        explanation = "Score below threshold — revisions required"

    record = DecisionRecord(
        scroll_id=scroll_id,
        decision=decision,
        rule_evaluations=evals,
        review_summary=review_summary,
        explanation=explanation,
    )

    # Persist decision record
    await db.execute(
        """
        INSERT INTO decision_records (
            decision_id, scroll_id, decision, rule_evaluations,
            review_summary, explanation, decided_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.decision_id,
            record.scroll_id,
            record.decision,
            to_json([e.model_dump() for e in record.rule_evaluations]),
            to_json(record.review_summary),
            record.explanation,
            record.decided_at.isoformat(),
        ),
    )

    # Apply decision to scroll
    status_map = {
        "accept": ScrollStatus.REPRO_CHECK,  # Goes to reproducibility check before publish
        "reject": ScrollStatus.REJECTED,
        "revisions_required": ScrollStatus.REVISIONS_REQUIRED,
    }
    new_status = status_map.get(decision)

    if new_status:
        await db.execute(
            "UPDATE scrolls SET status = ?, decision_record_id = ?, updated_at = ? WHERE scroll_id = ?",
            (new_status.value, record.decision_id, record.decided_at.isoformat(), scroll_id),
        )

    await db.commit()

    await log_event(
        db,
        AuditAction.DECISION_MADE,
        actor_id="policy_engine",
        target_id=scroll_id,
        target_type="scroll",
        details={
            "decision": decision,
            "decision_id": record.decision_id,
            "explanation": explanation,
        },
    )

    return record


async def get_decision_trace(
    db: aiosqlite.Connection,
    scroll_id: str,
) -> list[dict[str, Any]]:
    """Get all decision records for a scroll (full audit trail)."""
    async with db.execute(
        "SELECT * FROM decision_records WHERE scroll_id = ? ORDER BY decided_at",
        (scroll_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]
