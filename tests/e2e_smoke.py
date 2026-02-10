"""End-to-end smoke test: full publishing pipeline."""

import asyncio
import os
import sys
import tempfile

# Use a temp database so we don't pollute real data
os.environ["ALEXANDRIA_DATA"] = tempfile.mkdtemp(prefix="alexandria_test_")

from alexandria.database import get_db
from alexandria.models import (
    Claim,
    ReviewRecommendation,
    ReviewScores,
    ReviewSubmission,
    ScholarCreate,
    ScrollStatus,
    ScrollSubmission,
    ScrollType,
    SuggestedEdit,
)
from alexandria.scholar_service import register_scholar
from alexandria.scroll_service import submit_scroll, get_scroll
from alexandria.review_service import submit_review
from alexandria.policy_engine import evaluate_scroll
from alexandria.reproducibility_service import process_repro_gate


async def main():
    db = await get_db()
    print("=== E2E Smoke Test ===")
    print()

    # 1. Register scholars
    author = await register_scholar(db, ScholarCreate(name="Claude", affiliation="Anthropic"))
    reviewer1 = await register_scholar(db, ScholarCreate(name="GPT-4o", affiliation="OpenAI"))
    reviewer2 = await register_scholar(db, ScholarCreate(name="Gemini", affiliation="Google"))
    print(f"1. Registered scholars: {author.name}, {reviewer1.name}, {reviewer2.name}")

    # 2. Submit a scroll
    submission = ScrollSubmission(
        title="Semantic Caching for LLM Inference",
        abstract="A" * 80 + " We propose a semantic caching strategy that reduces LLM inference costs by 40%.",
        content="B" * 150 + " Introduction: Large language models are expensive to run. We propose caching semantically similar queries to reduce cost and latency.",
        domain="software-engineering",
        scroll_type=ScrollType.PAPER,
        keywords=["caching", "llm", "inference", "optimization"],
        authors=[author.scholar_id],
        claims=[Claim(statement="Semantic caching reduces LLM inference cost by 40%", evidence_type="empirical")],
    )
    scroll, errors = await submit_scroll(db, submission, author.scholar_id)
    assert scroll is not None, "Scroll should be created"
    assert len(errors) == 0, f"Should pass screening: {[e.to_dict() for e in errors]}"
    assert scroll.status == ScrollStatus.UNDER_REVIEW
    print(f"2. Submitted: {scroll.scroll_id} — status={scroll.status.value}")

    # 3. Submit peer reviews
    r1, errs1 = await submit_review(db, reviewer1.scholar_id, ReviewSubmission(
        scroll_id=scroll.scroll_id,
        scores=ReviewScores(originality=8, methodology=7, significance=8, clarity=7, overall=7),
        recommendation=ReviewRecommendation.ACCEPT,
        comments_to_authors="Strong contribution. Well-structured.",
        suggested_edits=[SuggestedEdit(
            section="Introduction",
            original_text="expensive to run",
            proposed_text="computationally expensive at scale",
            rationale="More precise",
        )],
    ))
    assert r1 is not None, f"Review 1 should succeed: {errs1}"
    print(f"3a. Review 1: {r1.recommendation.value}, overall={r1.scores.overall}")

    r2, errs2 = await submit_review(db, reviewer2.scholar_id, ReviewSubmission(
        scroll_id=scroll.scroll_id,
        scores=ReviewScores(originality=7, methodology=6, significance=7, clarity=8, overall=7),
        recommendation=ReviewRecommendation.MINOR_REVISIONS,
        comments_to_authors="Good work. Add more baselines.",
    ))
    assert r2 is not None, f"Review 2 should succeed: {errs2}"
    print(f"3b. Review 2: {r2.recommendation.value}, overall={r2.scores.overall}")

    # 4. Policy engine decides
    decision = await evaluate_scroll(db, scroll.scroll_id)
    assert decision is not None
    print(f"4. Decision: {decision.decision} — {decision.explanation}")

    # 5. Check status after decision
    scroll = await get_scroll(db, scroll.scroll_id)
    print(f"5. Status after decision: {scroll.status.value}")

    # If accepted, goes to repro_check
    if scroll.status == ScrollStatus.REPRO_CHECK:
        passed, reason = await process_repro_gate(db, scroll.scroll_id)
        print(f"   Repro gate: passed={passed}, reason={reason}")
        scroll = await get_scroll(db, scroll.scroll_id)

    print(f"6. Final status: {scroll.status.value}")
    print()

    # Verify conflict of interest check (use a fresh scroll for this test)
    sub2 = ScrollSubmission(
        title="Another Paper on LLM Optimization",
        abstract="A" * 80 + " This paper explores alternative optimization strategies for LLM inference.",
        content="B" * 150 + " Introduction: We build on prior caching work to explore further optimization.",
        domain="software-engineering",
        scroll_type=ScrollType.PAPER,
        keywords=["llm", "optimization"],
        authors=[author.scholar_id],
        claims=[Claim(statement="Method X reduces latency by 30%", evidence_type="empirical")],
    )
    scroll2, _ = await submit_scroll(db, sub2, author.scholar_id)
    _, conflicts = await submit_review(db, author.scholar_id, ReviewSubmission(
        scroll_id=scroll2.scroll_id,
        scores=ReviewScores(originality=10, methodology=10, significance=10, clarity=10, overall=10),
        recommendation=ReviewRecommendation.ACCEPT,
        comments_to_authors="Perfect!",
    ))
    assert "reviewer_is_author" in conflicts, f"Author should not be able to review own scroll, got: {conflicts}"
    print("7. Conflict of interest check: PASSED (author blocked from self-review)")

    print()
    print("=== ALL E2E CHECKS PASSED ===")
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
