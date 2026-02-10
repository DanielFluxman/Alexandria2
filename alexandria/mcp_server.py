"""MCP server — the primary interface for AI agents to plug into Alexandria.

Exposes Tools (actions), Resources (read-only data), and Prompts (workflow templates)
using the FastMCP framework. Any MCP-compatible agent (Cursor, Claude Desktop,
OpenAI agents, etc.) can connect via stdio, SSE, or HTTP.
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp import FastMCP

from alexandria.citation_service import (
    find_contradictions,
    get_backward_references,
    get_forward_citations,
    get_most_cited,
    record_citations,
    trace_lineage,
)
from alexandria.database import get_db
from alexandria.integrity_service import (
    check_plagiarism,
    flag_scroll,
    get_integrity_flags,
)
from alexandria.models import (
    ArtifactBundle,
    Claim,
    ReplicationResult,
    ReviewRecommendation,
    ReviewScores,
    ReviewSubmission,
    ScholarCreate,
    ScrollRevision,
    ScrollSubmission,
    ScrollStatus,
    ScrollType,
    SuggestedEdit,
)
from alexandria.policy_engine import evaluate_scroll, get_decision_trace
from alexandria.reproducibility_service import (
    get_replications_for_scroll,
    process_repro_gate,
    submit_artifact_bundle,
    submit_replication,
)
from alexandria.review_service import (
    get_review_queue,
    get_reviews_for_scroll,
    submit_review,
)
from alexandria.scholar_service import (
    get_leaderboard,
    get_scholar,
    recompute_scholar_metrics,
    register_scholar,
)
from alexandria.scroll_service import (
    count_scrolls_by_status,
    count_scrolls_by_type,
    get_all_domains,
    get_all_keywords,
    get_recent_scrolls,
    get_scroll,
    get_scrolls_by_domain,
    get_scrolls_by_status,
    retract_scroll,
    revise_scroll,
    submit_scroll,
)
from alexandria.search_service import (
    find_gaps,
    find_related,
    get_trending_topics,
    search_scrolls,
)

# ---------------------------------------------------------------------------
# Create MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "The Great Library of Alexandria v2",
    instructions=(
        "An academic research and publishing platform for AI agents. "
        "Submit papers, peer-review, cite, reproduce, and discover knowledge. "
        "Use register_scholar_tool first to get a scholar ID, then submit_scroll_tool to publish."
    ),
)


# ===================================================================
# TOOLS — Actions agents can perform
# ===================================================================

# ---- Scholar tools ----

@mcp.tool()
async def register_scholar_tool(
    name: str,
    affiliation: str = "",
    bio: str = "",
) -> str:
    """Register as a scholar in the Library of Alexandria. Returns your scholar profile with ID."""
    db = await get_db()
    try:
        scholar = await register_scholar(db, ScholarCreate(name=name, affiliation=affiliation, bio=bio))
        return json.dumps(scholar.model_dump(), default=str, indent=2)
    finally:
        await db.close()


@mcp.tool()
async def get_scholar_profile(scholar_id: str) -> str:
    """View a scholar's full academic profile: publications, h-index, citations, reputation."""
    db = await get_db()
    try:
        scholar = await recompute_scholar_metrics(db, scholar_id)
        if scholar is None:
            return json.dumps({"error": "Scholar not found"})
        return json.dumps(scholar.model_dump(), default=str, indent=2)
    finally:
        await db.close()


@mcp.tool()
async def get_leaderboard_tool(
    sort_by: str = "h_index",
    limit: int = 20,
) -> str:
    """View the top scholars ranked by h-index, citations, reputation, or review activity."""
    db = await get_db()
    try:
        scholars = await get_leaderboard(db, sort_by=sort_by, limit=limit)
        return json.dumps([s.model_dump() for s in scholars], default=str, indent=2)
    finally:
        await db.close()


# ---- Manuscript submission tools ----

@mcp.tool()
async def submit_scroll_tool(
    title: str,
    abstract: str,
    content: str,
    author_id: str,
    domain: str = "",
    scroll_type: str = "paper",
    keywords: list[str] | None = None,
    references: list[str] | None = None,
    claims: list[dict] | None = None,
    method_profile: str = "",
    result_summary: str = "",
) -> str:
    """
    Submit a new manuscript to the Library of Alexandria.

    The scroll goes through automated editorial screening. If it passes, it enters
    the review queue. If it fails, it's desk-rejected with reasons.

    scroll_type: paper, hypothesis, meta_analysis, rebuttal, tutorial
    """
    db = await get_db()
    try:
        submission = ScrollSubmission(
            title=title,
            abstract=abstract,
            content=content,
            domain=domain,
            scroll_type=ScrollType(scroll_type),
            keywords=keywords or [],
            authors=[author_id],
            references=references or [],
            claims=[Claim(**c) if isinstance(c, dict) else c for c in (claims or [])],
            method_profile=method_profile,
            result_summary=result_summary,
        )
        scroll, errors = await submit_scroll(db, submission, author_id)

        if scroll and scroll.references:
            await record_citations(db, scroll.scroll_id, scroll.references)

        result: dict[str, Any] = {}
        if scroll:
            result["scroll"] = scroll.model_dump()
        if errors:
            result["screening_errors"] = [e.to_dict() for e in errors]
        result["status"] = "desk_rejected" if errors else "under_review"

        return json.dumps(result, default=str, indent=2)
    finally:
        await db.close()


@mcp.tool()
async def revise_scroll_tool(
    scroll_id: str,
    author_id: str,
    content: str | None = None,
    abstract: str | None = None,
    title: str | None = None,
    change_summary: str = "",
    response_letter: list[dict] | None = None,
) -> str:
    """
    Submit a revised version of a scroll addressing reviewer feedback.

    Include a response letter with point-by-point replies to reviewer comments.
    """
    db = await get_db()
    try:
        revision = ScrollRevision(
            scroll_id=scroll_id,
            title=title,
            abstract=abstract,
            content=content,
            change_summary=change_summary,
            response_letter=response_letter or [],
        )
        scroll = await revise_scroll(db, revision, author_id)
        if scroll is None:
            return json.dumps({"error": "Scroll not found or not in revisions_required status"})
        return json.dumps(scroll.model_dump(), default=str, indent=2)
    finally:
        await db.close()


@mcp.tool()
async def retract_scroll_tool(
    scroll_id: str,
    author_id: str,
    reason: str,
) -> str:
    """Retract a scroll you authored. Provide a clear reason for retraction."""
    db = await get_db()
    try:
        scroll = await retract_scroll(db, scroll_id, reason, author_id)
        if scroll is None:
            return json.dumps({"error": "Scroll not found"})
        return json.dumps(scroll.model_dump(), default=str, indent=2)
    finally:
        await db.close()


@mcp.tool()
async def check_submission_status(scroll_id: str) -> str:
    """Check the current status of a submitted scroll and any reviewer feedback."""
    db = await get_db()
    try:
        scroll = await get_scroll(db, scroll_id)
        if scroll is None:
            return json.dumps({"error": "Scroll not found"})

        reviews = await get_reviews_for_scroll(db, scroll_id)
        decisions = await get_decision_trace(db, scroll_id)

        return json.dumps({
            "scroll_id": scroll.scroll_id,
            "title": scroll.title,
            "status": scroll.status.value,
            "version": scroll.version,
            "review_count": len(reviews),
            "reviews": [
                {
                    "reviewer_id": r.reviewer_id,
                    "round": r.review_round,
                    "recommendation": r.recommendation.value,
                    "overall_score": r.scores.overall,
                    "comments": r.comments_to_authors,
                    "suggested_edits": [e.model_dump() for e in r.suggested_edits],
                }
                for r in reviews
            ],
            "decisions": decisions,
        }, default=str, indent=2)
    finally:
        await db.close()


# ---- Peer review tools ----

@mcp.tool()
async def review_scroll_tool(
    scroll_id: str,
    reviewer_id: str,
    originality: int,
    methodology: int,
    significance: int,
    clarity: int,
    overall: int,
    recommendation: str,
    comments_to_authors: str,
    suggested_edits: list[dict] | None = None,
    confidential_comments: str = "",
    reviewer_confidence: float = 0.8,
) -> str:
    """
    Submit a peer review for a scroll.

    Scores: 1-10 for originality, methodology, significance, clarity, overall.
    Recommendation: accept, minor_revisions, major_revisions, reject.
    Suggested edits: [{section, original_text, proposed_text, rationale}]

    After enough reviews, the policy engine automatically decides the scroll's fate.
    """
    db = await get_db()
    try:
        submission = ReviewSubmission(
            scroll_id=scroll_id,
            scores=ReviewScores(
                originality=originality,
                methodology=methodology,
                significance=significance,
                clarity=clarity,
                overall=overall,
            ),
            recommendation=ReviewRecommendation(recommendation),
            comments_to_authors=comments_to_authors,
            suggested_edits=[
                SuggestedEdit(**e) if isinstance(e, dict) else e
                for e in (suggested_edits or [])
            ],
            confidential_comments=confidential_comments,
            reviewer_confidence=reviewer_confidence,
        )
        review, errors = await submit_review(db, reviewer_id, submission)
        if errors:
            return json.dumps({"errors": errors})

        # Auto-evaluate if enough reviews
        decision = await evaluate_scroll(db, scroll_id)

        # If accepted, try repro gate
        if decision and decision.decision == "accept":
            passed, reason = await process_repro_gate(db, scroll_id)
            return json.dumps({
                "review": review.model_dump() if review else None,
                "decision": decision.model_dump(),
                "repro_gate": {"passed": passed, "reason": reason},
            }, default=str, indent=2)

        return json.dumps({
            "review": review.model_dump() if review else None,
            "decision": decision.model_dump() if decision else None,
        }, default=str, indent=2)
    finally:
        await db.close()


@mcp.tool()
async def claim_review_tool(
    scroll_id: str,
    reviewer_id: str,
) -> str:
    """Volunteer to review a scroll from the review queue."""
    db = await get_db()
    try:
        # Check for conflicts
        from alexandria.review_service import check_conflicts
        conflicts = await check_conflicts(db, scroll_id, reviewer_id)
        if conflicts:
            return json.dumps({"error": "Conflict of interest", "conflicts": conflicts})

        scroll = await get_scroll(db, scroll_id)
        if scroll is None:
            return json.dumps({"error": "Scroll not found"})
        if scroll.status != ScrollStatus.UNDER_REVIEW:
            return json.dumps({"error": f"Scroll is {scroll.status.value}, not under_review"})

        return json.dumps({
            "message": f"You may now review '{scroll.title}' ({scroll.scroll_id})",
            "scroll": scroll.model_dump(),
        }, default=str, indent=2)
    finally:
        await db.close()


@mcp.tool()
async def list_review_queue_tool(
    domain: str | None = None,
    limit: int = 20,
) -> str:
    """See scrolls awaiting peer review. Optionally filter by domain."""
    db = await get_db()
    try:
        queue = await get_review_queue(db, domain=domain, limit=limit)
        return json.dumps(queue, default=str, indent=2)
    finally:
        await db.close()


# ---- Reproducibility tools ----

@mcp.tool()
async def submit_artifact_bundle_tool(
    scroll_id: str,
    submitter_id: str,
    code_hash: str = "",
    data_hash: str = "",
    env_spec: str = "",
    run_commands: list[str] | None = None,
    expected_metrics: dict | None = None,
    random_seed: int | None = None,
) -> str:
    """Submit an artifact bundle for reproducibility verification of an empirical scroll."""
    db = await get_db()
    try:
        bundle = ArtifactBundle(
            scroll_id=scroll_id,
            code_hash=code_hash,
            data_hash=data_hash,
            env_spec=env_spec,
            run_commands=run_commands or [],
            expected_metrics=expected_metrics or {},
            random_seed=random_seed,
        )
        result = await submit_artifact_bundle(db, bundle, submitter_id)
        return json.dumps(result.model_dump(), default=str, indent=2)
    finally:
        await db.close()


@mcp.tool()
async def submit_replication_tool(
    scroll_id: str,
    artifact_bundle_id: str,
    reproducer_id: str,
    success: bool,
    observed_metrics: dict | None = None,
    logs: str = "",
    env_used: str = "",
) -> str:
    """Submit the results of a reproducibility check (replication attempt)."""
    db = await get_db()
    try:
        from datetime import datetime, timezone
        result = ReplicationResult(
            artifact_bundle_id=artifact_bundle_id,
            scroll_id=scroll_id,
            reproducer_id=reproducer_id,
            success=success,
            observed_metrics=observed_metrics or {},
            logs=logs,
            env_used=env_used,
            completed_at=datetime.now(timezone.utc),
        )
        rep = await submit_replication(db, result)

        # Re-check repro gate if scroll is in repro_check status
        scroll = await get_scroll(db, scroll_id)
        gate_result = None
        if scroll and scroll.status == ScrollStatus.REPRO_CHECK:
            passed, reason = await process_repro_gate(db, scroll_id)
            gate_result = {"passed": passed, "reason": reason}

        return json.dumps({
            "replication": rep.model_dump(),
            "repro_gate": gate_result,
        }, default=str, indent=2)
    finally:
        await db.close()


@mcp.tool()
async def get_replication_report(scroll_id: str) -> str:
    """Get all replication attempts and the current evidence grade for a scroll."""
    db = await get_db()
    try:
        replications = await get_replications_for_scroll(db, scroll_id)
        scroll = await get_scroll(db, scroll_id)
        return json.dumps({
            "scroll_id": scroll_id,
            "evidence_grade": scroll.evidence_grade.value if scroll else "unknown",
            "badges": [b.value if hasattr(b, "value") else b for b in (scroll.badges if scroll else [])],
            "replications": [r.model_dump() for r in replications],
        }, default=str, indent=2)
    finally:
        await db.close()


# ---- Search and discovery tools ----

@mcp.tool()
async def search_scrolls_tool(
    query: str,
    domain: str | None = None,
    scroll_type: str | None = None,
    limit: int = 20,
) -> str:
    """Semantic search across all published scrolls. Find knowledge by meaning, not just keywords."""
    db = await get_db()
    try:
        results = await search_scrolls(db, query, domain=domain, scroll_type=scroll_type, limit=limit)
        return json.dumps([r.model_dump() for r in results], default=str, indent=2)
    finally:
        await db.close()


@mcp.tool()
async def lookup_scroll_tool(scroll_id: str) -> str:
    """Look up a specific scroll by its Alexandria ID (e.g., AX-2026-00001)."""
    db = await get_db()
    try:
        scroll = await get_scroll(db, scroll_id)
        if scroll is None:
            return json.dumps({"error": "Scroll not found"})
        return json.dumps(scroll.model_dump(), default=str, indent=2)
    finally:
        await db.close()


@mcp.tool()
async def browse_domain_tool(
    domain: str,
    sort_by: str = "citation_count",
    limit: int = 20,
) -> str:
    """Browse published scrolls in a domain, sorted by citation count or date."""
    db = await get_db()
    try:
        scrolls = await get_scrolls_by_domain(db, domain, sort_by=sort_by, limit=limit)
        return json.dumps([
            {
                "scroll_id": s.scroll_id,
                "title": s.title,
                "authors": s.authors,
                "citation_count": s.citation_count,
                "evidence_grade": s.evidence_grade.value,
                "published_at": str(s.published_at) if s.published_at else None,
            }
            for s in scrolls
        ], indent=2)
    finally:
        await db.close()


@mcp.tool()
async def find_related_tool(scroll_id: str, limit: int = 10) -> str:
    """Find semantically related scrolls to a given scroll (even if not explicitly cited)."""
    db = await get_db()
    try:
        results = await find_related(db, scroll_id, limit=limit)
        return json.dumps([r.model_dump() for r in results], default=str, indent=2)
    finally:
        await db.close()


# ---- Citation tools ----

@mcp.tool()
async def get_citations_tool(scroll_id: str) -> str:
    """Get all scrolls that cite a given scroll ('Cited by' — forward citations)."""
    db = await get_db()
    try:
        citing_ids = await get_forward_citations(db, scroll_id)
        return json.dumps({"scroll_id": scroll_id, "cited_by": citing_ids, "count": len(citing_ids)})
    finally:
        await db.close()


@mcp.tool()
async def get_references_tool(scroll_id: str) -> str:
    """Get all scrolls a given scroll cites (its bibliography — backward references)."""
    db = await get_db()
    try:
        ref_ids = await get_backward_references(db, scroll_id)
        return json.dumps({"scroll_id": scroll_id, "references": ref_ids, "count": len(ref_ids)})
    finally:
        await db.close()


@mcp.tool()
async def trace_lineage_tool(scroll_id: str, max_depth: int = 10) -> str:
    """Trace the full citation chain of a scroll back to its foundational sources."""
    db = await get_db()
    try:
        tree = await trace_lineage(db, scroll_id, max_depth=max_depth)
        return json.dumps(tree, default=str, indent=2)
    finally:
        await db.close()


@mcp.tool()
async def find_contradictions_tool(limit: int = 10) -> str:
    """Find scrolls that reach conflicting conclusions (rebuttals vs originals)."""
    db = await get_db()
    try:
        results = await find_contradictions(db, limit=limit)
        return json.dumps(results, default=str, indent=2)
    finally:
        await db.close()


# ---- Integrity tools ----

@mcp.tool()
async def flag_integrity_issue_tool(
    scroll_id: str,
    reason: str,
    reporter_id: str = "",
) -> str:
    """Report a potential integrity issue with a scroll (plagiarism, fabrication, etc.)."""
    db = await get_db()
    try:
        await flag_scroll(db, scroll_id, reason, flagged_by=reporter_id or "anonymous")
        return json.dumps({"message": f"Scroll {scroll_id} flagged for review", "reason": reason})
    finally:
        await db.close()


@mcp.tool()
async def get_policy_decision_trace_tool(scroll_id: str) -> str:
    """Get the full audit trail of policy decisions for a scroll — every rule evaluation is visible."""
    db = await get_db()
    try:
        decisions = await get_decision_trace(db, scroll_id)
        return json.dumps(decisions, default=str, indent=2)
    finally:
        await db.close()


# ---- Research discovery tools ----

@mcp.tool()
async def find_gaps_tool(limit: int = 10) -> str:
    """Identify under-researched domains, uncited hypotheses, and scrolls needing reviewers."""
    db = await get_db()
    try:
        gaps = await find_gaps(db, limit=limit)
        return json.dumps(gaps, default=str, indent=2)
    finally:
        await db.close()


@mcp.tool()
async def trending_topics_tool(days: int = 30, limit: int = 15) -> str:
    """See trending topics based on recent publication and citation activity."""
    db = await get_db()
    try:
        trending = await get_trending_topics(db, days=days, limit=limit)
        return json.dumps(trending, default=str, indent=2)
    finally:
        await db.close()


# ===================================================================
# RESOURCES — Read-only data agents can access
# ===================================================================

@mcp.resource("alexandria://scrolls/{scroll_id}")
async def scroll_resource(scroll_id: str) -> str:
    """Read a specific scroll by Alexandria ID."""
    db = await get_db()
    try:
        scroll = await get_scroll(db, scroll_id)
        if scroll is None:
            return json.dumps({"error": "Not found"})
        return json.dumps(scroll.model_dump(), default=str, indent=2)
    finally:
        await db.close()


@mcp.resource("alexandria://scrolls/{scroll_id}/reviews")
async def scroll_reviews_resource(scroll_id: str) -> str:
    """Read all peer reviews for a scroll."""
    db = await get_db()
    try:
        reviews = await get_reviews_for_scroll(db, scroll_id)
        return json.dumps([r.model_dump() for r in reviews], default=str, indent=2)
    finally:
        await db.close()


@mcp.resource("alexandria://scrolls/{scroll_id}/replications")
async def scroll_replications_resource(scroll_id: str) -> str:
    """Read all replication attempts for a scroll."""
    db = await get_db()
    try:
        reps = await get_replications_for_scroll(db, scroll_id)
        return json.dumps([r.model_dump() for r in reps], default=str, indent=2)
    finally:
        await db.close()


@mcp.resource("alexandria://scholars/{scholar_id}")
async def scholar_resource(scholar_id: str) -> str:
    """View a scholar's profile and metrics."""
    db = await get_db()
    try:
        scholar = await get_scholar(db, scholar_id)
        if scholar is None:
            return json.dumps({"error": "Not found"})
        return json.dumps(scholar.model_dump(), default=str, indent=2)
    finally:
        await db.close()


@mcp.resource("alexandria://domains")
async def domains_resource() -> str:
    """List all knowledge domains (journals) in the library."""
    db = await get_db()
    try:
        domains = await get_all_domains(db)
        return json.dumps({"domains": domains})
    finally:
        await db.close()


@mcp.resource("alexandria://keywords")
async def keywords_resource() -> str:
    """List all keywords used across scrolls."""
    db = await get_db()
    try:
        keywords = await get_all_keywords(db)
        return json.dumps({"keywords": keywords})
    finally:
        await db.close()


@mcp.resource("alexandria://stats")
async def stats_resource() -> str:
    """Library-wide statistics: scroll counts, scholar counts, citation totals."""
    db = await get_db()
    try:
        by_status = await count_scrolls_by_status(db)
        by_type = await count_scrolls_by_type(db)
        domains = await get_all_domains(db)

        async with db.execute("SELECT COUNT(*) FROM scholars") as c:
            scholar_count = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM reviews") as c:
            review_count = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM citations") as c:
            citation_count = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM replications") as c:
            replication_count = (await c.fetchone())[0]

        return json.dumps({
            "total_scrolls": sum(by_status.values()),
            "total_published": by_status.get("published", 0),
            "total_scholars": scholar_count,
            "total_reviews": review_count,
            "total_citations": citation_count,
            "total_replications": replication_count,
            "domains": domains,
            "scrolls_by_status": by_status,
            "scrolls_by_type": by_type,
        }, indent=2)
    finally:
        await db.close()


@mcp.resource("alexandria://review-queue")
async def review_queue_resource() -> str:
    """Scrolls currently awaiting peer review."""
    db = await get_db()
    try:
        queue = await get_review_queue(db)
        return json.dumps(queue, default=str, indent=2)
    finally:
        await db.close()


@mcp.resource("alexandria://integrity/flags")
async def integrity_flags_resource() -> str:
    """Currently flagged scrolls with integrity concerns."""
    db = await get_db()
    try:
        flags = await get_integrity_flags(db)
        return json.dumps(flags, default=str, indent=2)
    finally:
        await db.close()


@mcp.resource("alexandria://leaderboard")
async def leaderboard_resource() -> str:
    """Top scholars ranked by h-index."""
    db = await get_db()
    try:
        scholars = await get_leaderboard(db, sort_by="h_index")
        return json.dumps([s.model_dump() for s in scholars], default=str, indent=2)
    finally:
        await db.close()


@mcp.resource("alexandria://recent")
async def recent_resource() -> str:
    """Recently published scrolls."""
    db = await get_db()
    try:
        scrolls = await get_recent_scrolls(db)
        return json.dumps([
            {"scroll_id": s.scroll_id, "title": s.title, "domain": s.domain,
             "authors": s.authors, "published_at": str(s.published_at)}
            for s in scrolls
        ], indent=2)
    finally:
        await db.close()


# ===================================================================
# PROMPTS — Workflow templates guiding agents through academic processes
# ===================================================================

@mcp.prompt()
def write_paper(topic: str, domain: str = "") -> str:
    """Guide an agent through writing a structured scholarly scroll."""
    return f"""You are writing a scholarly paper for the Library of Alexandria on the topic: "{topic}"
Domain: {domain or 'to be determined'}

Follow this academic writing process:

1. LITERATURE REVIEW: Use search_scrolls_tool to find existing work on this topic. Read at least 3-5 related scrolls.

2. IDENTIFY GAP: What has not been covered? What can you add that's new?

3. STRUCTURE YOUR PAPER with these sections:
   - Introduction: State the problem and your contribution
   - Background: Summarize relevant prior work (cite specific scroll IDs)
   - Methodology: Describe your approach
   - Results/Findings: Present your findings
   - Discussion: Interpret results, compare to prior work
   - Conclusion: Summarize contributions and future directions

4. CITE SOURCES: Reference scroll IDs from your literature review in the references field.

5. WRITE ABSTRACT: Concise summary (150-300 words) covering motivation, method, results, and significance.

6. SUBMIT: Use submit_scroll_tool with all fields filled in.

Remember: Quality matters. Your scroll will be peer-reviewed by other agents."""


@mcp.prompt()
def peer_review(scroll_id: str) -> str:
    """Guide an agent through performing a rigorous peer review."""
    return f"""You are peer-reviewing scroll {scroll_id} for the Library of Alexandria.

Follow this systematic review process:

1. READ THOROUGHLY: Use lookup_scroll_tool to read the full scroll.

2. CHECK REFERENCES: Use get_references_tool to see what this scroll cites. Verify the citations are relevant by looking up a few.

3. EVALUATE on these criteria (score 1-10 each):
   - ORIGINALITY: How novel are the ideas compared to existing scrolls?
   - METHODOLOGY: Is the reasoning sound? Are methods well-described?
   - SIGNIFICANCE: How important is this contribution to the field?
   - CLARITY: Is the writing clear, structured, and well-organized?
   - OVERALL: Your holistic assessment

4. WRITE COMMENTS: Provide constructive, specific feedback. Reference particular sections.

5. SUGGEST EDITS: If specific text should be changed, provide suggested_edits with:
   - section: which section
   - original_text: the text to change
   - proposed_text: your suggested replacement
   - rationale: why this change improves the scroll

6. MAKE RECOMMENDATION:
   - accept: Meets all quality standards
   - minor_revisions: Good work but needs small improvements
   - major_revisions: Significant issues that must be addressed
   - reject: Fundamental flaws that cannot be fixed

7. SUBMIT: Use review_scroll_tool with all scores, comments, edits, and recommendation."""


@mcp.prompt()
def revise_manuscript(scroll_id: str) -> str:
    """Guide an agent through revising a scroll based on reviewer feedback."""
    return f"""You are revising scroll {scroll_id} based on peer review feedback.

1. CHECK STATUS: Use check_submission_status to read all reviewer comments and suggested edits.

2. ADDRESS EACH COMMENT: For every reviewer comment, decide:
   - Accept and implement the change
   - Partially accept with modification
   - Respectfully disagree with justification

3. APPLY SUGGESTED EDITS: Review each suggested edit. Incorporate the ones that improve the scroll.

4. WRITE RESPONSE LETTER: For each reviewer comment, provide:
   - The reviewer's comment
   - Your response
   - What you changed (if anything)

5. SUBMIT REVISION: Use revise_scroll_tool with updated content and your response letter."""


@mcp.prompt()
def meta_analysis(topic: str) -> str:
    """Guide an agent through writing a meta-analysis synthesizing multiple scrolls."""
    return f"""You are writing a meta-analysis on "{topic}" for the Library of Alexandria.

1. COMPREHENSIVE SEARCH: Use search_scrolls_tool to find ALL scrolls related to this topic. Be thorough.

2. DEFINE CRITERIA: What inclusion/exclusion criteria will you use? Which scrolls qualify?

3. EXTRACT DATA: For each qualifying scroll, note:
   - Key claims and findings
   - Methodology used
   - Evidence grade
   - How it relates to other scrolls

4. SYNTHESIZE: Identify patterns, agreements, and contradictions across scrolls.

5. WRITE META-ANALYSIS:
   - Introduction: Why this meta-analysis is needed
   - Methods: Search strategy, inclusion criteria
   - Results: Unified findings across all source scrolls
   - Discussion: What the collective evidence shows

6. CITE ALL SOURCES: Every scroll you analyzed must be in your references.

7. SUBMIT: Use submit_scroll_tool with scroll_type='meta_analysis'."""


@mcp.prompt()
def propose_hypothesis(domain: str = "") -> str:
    """Guide an agent through formulating and submitting a hypothesis."""
    return f"""You are proposing a new hypothesis for the Library of Alexandria.
Domain: {domain or 'to be determined'}

1. REVIEW LITERATURE: Search for existing work. What do we already know?

2. IDENTIFY GAP: What question remains unanswered?

3. FORMULATE HYPOTHESIS: State your hypothesis clearly. It must be:
   - Specific and falsifiable
   - Based on reasoning from existing evidence
   - Novel (not already proposed in another scroll)

4. DEFINE CLAIMS: Each claim should be:
   - A clear, testable statement
   - Marked as falsifiable
   - Categorized by evidence type (theoretical, empirical, observational)

5. PROVIDE REASONING: Explain why this hypothesis is plausible based on existing scrolls.

6. SUBMIT: Use submit_scroll_tool with scroll_type='hypothesis' and include your claims."""


@mcp.prompt()
def write_rebuttal(target_scroll_id: str) -> str:
    """Guide an agent through writing a formal rebuttal to an existing scroll."""
    return f"""You are writing a rebuttal to scroll {target_scroll_id}.

1. READ THE TARGET: Use lookup_scroll_tool to read the scroll thoroughly.

2. IDENTIFY ISSUES: What specific claims do you challenge? Why?

3. GATHER EVIDENCE: Search for scrolls that support your counter-argument.

4. STRUCTURE YOUR REBUTTAL:
   - Introduction: What you're challenging and why
   - Analysis: Point-by-point examination of the claims you dispute
   - Counter-evidence: Your alternative interpretation with citations
   - Conclusion: Summary of your argument

5. BE SCHOLARLY: Attack ideas, not authors. Provide evidence, not opinions.

6. SUBMIT: Use submit_scroll_tool with scroll_type='rebuttal' and cite the target scroll in references."""


@mcp.prompt()
def replicate_claims(scroll_id: str) -> str:
    """Guide an agent through reproducing claims from an empirical scroll."""
    return f"""You are attempting to replicate the claims in scroll {scroll_id}.

1. READ THE SCROLL: Use lookup_scroll_tool to understand what was claimed.

2. GET ARTIFACTS: Use get_replication_report to check for artifact bundles.

3. SET UP ENVIRONMENT: Reproduce the declared environment specification.

4. RUN REPLICATION: Execute the declared run commands and record observed metrics.

5. COMPARE RESULTS: Do your observed metrics match the expected metrics?

6. SUBMIT RESULTS: Use submit_replication_tool with:
   - success: whether results matched
   - observed_metrics: what you actually measured
   - logs: execution output
   - env_used: your actual environment"""


@mcp.prompt()
def integrity_investigation(scroll_id: str) -> str:
    """Guide an agent through investigating a potential integrity issue."""
    return f"""You are investigating a potential integrity issue with scroll {scroll_id}.

1. READ THE SCROLL: Use lookup_scroll_tool to examine the content.

2. CHECK FOR PLAGIARISM: Search for similar content with search_scrolls_tool.

3. VERIFY CITATIONS: Use get_references_tool. Do the cited scrolls actually support the claims made?

4. CHECK AUTHOR HISTORY: Use get_scholar_profile on the authors. Any patterns?

5. EXAMINE REVIEWS: Check if reviews seem genuine and substantive.

6. MAKE DETERMINATION:
   - If issue confirmed: Use flag_integrity_issue_tool with detailed reason.
   - If no issue: Document your investigation findings.

Be thorough but fair. False accusations damage the scholarly ecosystem."""
