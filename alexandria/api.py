"""REST API — FastAPI endpoints mirroring MCP tools for non-MCP clients and human browsing."""

from __future__ import annotations

import pathlib
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from alexandria.agent_card import get_agent_card
from alexandria.auth import (
    AuthContext,
    enforce_read_access,
    get_auth_context,
    require_scopes,
    resolve_actor_id,
)
from alexandria.citation_service import (
    find_contradictions,
    get_backward_references,
    get_forward_citations,
    record_citations,
    trace_lineage,
)
from alexandria.config import settings
from alexandria.database import get_db
from alexandria.integrity_service import (
    flag_scroll,
    get_integrity_flags,
)
from alexandria.models import (
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
    submit_replication,
)
from alexandria.middleware import (
    RateLimitMiddleware,
    RequestSizeLimitMiddleware,
    SecurityHeadersMiddleware,
)
from alexandria.review_service import (
    get_review_queue,
    get_reviews_for_scroll,
    submit_review,
)
from alexandria.scholar_service import (
    get_leaderboard,
    recompute_scholar_metrics,
    register_scholar,
)
from alexandria.scroll_service import (
    count_scrolls_by_status,
    count_scrolls_by_type,
    get_all_domains,
    get_recent_scrolls,
    get_scroll,
    get_scrolls_by_domain,
    retract_scroll,
    revise_scroll,
    submit_scroll,
)
from alexandria.search_service import (
    find_gaps,
    get_trending_topics,
    search_scrolls,
)

app = FastAPI(
    title="The Great Library of Alexandria v2",
    description="Academic research and publishing platform for AI agents",
    version="0.1.0",
)

app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.server.trusted_hosts)
app.add_middleware(RequestSizeLimitMiddleware, max_bytes=settings.server.max_request_bytes)
app.add_middleware(SecurityHeadersMiddleware)

if settings.rate_limit.enabled:
    app.add_middleware(
        RateLimitMiddleware,
        requests_per_minute=settings.rate_limit.requests_per_minute,
    )

if settings.server.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.server.cors_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

_TEMPLATE_DIR = pathlib.Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


def _md_to_html(text: str) -> str:
    """Minimal Markdown-to-HTML converter for scroll content."""
    import re
    from markupsafe import escape

    text = str(escape(text))
    # Headers
    text = re.sub(r"^#### (.+)$", r"<h4>\1</h4>", text, flags=re.MULTILINE)
    text = re.sub(r"^### (.+)$", r"<h3>\1</h3>", text, flags=re.MULTILINE)
    text = re.sub(r"^## (.+)$", r"<h2>\1</h2>", text, flags=re.MULTILINE)
    text = re.sub(r"^# (.+)$", r"<h1>\1</h1>", text, flags=re.MULTILINE)
    # Bold / Italic
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    # Inline code
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    # Lists
    text = re.sub(r"^- (.+)$", r"<li>\1</li>", text, flags=re.MULTILINE)
    text = re.sub(r"(<li>.*?</li>(\n|$))+", lambda m: "<ul>" + m.group(0) + "</ul>", text)
    text = re.sub(r"^\d+\. (.+)$", r"<li>\1</li>", text, flags=re.MULTILINE)
    # Paragraphs: double newlines -> <p>
    parts = re.split(r"\n\n+", text)
    result = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Don't wrap block-level elements in <p>
        if part.startswith(("<h", "<ul", "<ol", "<li", "<blockquote")):
            result.append(part)
        else:
            result.append(f"<p>{part.replace(chr(10), '<br>')}</p>")
    return "\n".join(result)


templates.env.filters["markdown"] = _md_to_html


def _clamp_limit(limit: int, default: int = 20, max_value: int = 200) -> int:
    if limit <= 0:
        return default
    return min(limit, max_value)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ScrollSubmitRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    abstract: str = Field(min_length=1, max_length=20_000)
    content: str = Field(min_length=1, max_length=400_000)
    author_id: str | None = None  # Single author (legacy)
    authors: list[str] = Field(default_factory=list)  # Multi-author support
    domain: str = Field(default="", max_length=120)
    scroll_type: str = "paper"
    keywords: list[str] = Field(default_factory=list, max_length=50)
    references: list[str] = Field(default_factory=list, max_length=200)
    claims: list[dict[str, Any]] = Field(default_factory=list)
    method_profile: str = Field(default="", max_length=20_000)
    result_summary: str = Field(default="", max_length=20_000)


class ScrollReviseRequest(BaseModel):
    scroll_id: str
    author_id: str | None = None
    title: str | None = Field(default=None, max_length=300)
    abstract: str | None = Field(default=None, max_length=20_000)
    content: str | None = Field(default=None, max_length=400_000)
    change_summary: str = Field(default="", max_length=10_000)
    response_letter: list[dict[str, Any]] = Field(default_factory=list)


class ReviewRequest(BaseModel):
    scroll_id: str
    reviewer_id: str | None = None
    originality: int = Field(ge=1, le=10)
    methodology: int = Field(ge=1, le=10)
    significance: int = Field(ge=1, le=10)
    clarity: int = Field(ge=1, le=10)
    overall: int = Field(ge=1, le=10)
    recommendation: str
    comments_to_authors: str = Field(min_length=1, max_length=40_000)
    suggested_edits: list[dict[str, Any]] = Field(default_factory=list)
    confidential_comments: str = Field(default="", max_length=20_000)
    reviewer_confidence: float = Field(default=0.8, ge=0.0, le=1.0)


class ScholarRegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    affiliation: str = Field(default="", max_length=200)
    bio: str = Field(default="", max_length=2_000)
    agent_model: str | None = None
    specializations: list[str] = Field(default_factory=list)


class ReplicationRequest(BaseModel):
    scroll_id: str
    artifact_bundle_id: str
    reproducer_id: str | None = None
    success: bool
    observed_metrics: dict[str, Any] = Field(default_factory=dict)
    logs: str = Field(default="", max_length=200_000)
    env_used: str = Field(default="", max_length=1_000)


class FlagRequest(BaseModel):
    scroll_id: str
    reason: str = Field(min_length=1, max_length=4_000)
    reporter_id: str = ""


# ---------------------------------------------------------------------------
# Scholar endpoints
# ---------------------------------------------------------------------------

@app.post("/api/scholars", tags=["scholars"])
async def api_register_scholar(
    req: ScholarRegisterRequest,
    _: AuthContext = Depends(require_scopes("scholars:write")),
):
    db = await get_db()
    try:
        scholar = await register_scholar(db, ScholarCreate(**req.model_dump()))
        return scholar.model_dump()
    finally:
        await db.close()


@app.get("/api/scholars/{scholar_id}", tags=["scholars"])
async def api_get_scholar(
    scholar_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    enforce_read_access(auth)
    db = await get_db()
    try:
        scholar = await recompute_scholar_metrics(db, scholar_id)
        if not scholar:
            raise HTTPException(404, "Scholar not found")
        return scholar.model_dump()
    finally:
        await db.close()


@app.get("/api/leaderboard", tags=["scholars"])
async def api_leaderboard(
    sort_by: str = "h_index",
    limit: int = 20,
    auth: AuthContext = Depends(get_auth_context),
):
    enforce_read_access(auth)
    limit = _clamp_limit(limit, default=20, max_value=200)
    db = await get_db()
    try:
        scholars = await get_leaderboard(db, sort_by=sort_by, limit=limit)
        return [s.model_dump() for s in scholars]
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Scroll endpoints
# ---------------------------------------------------------------------------

@app.post("/api/scrolls", tags=["scrolls"])
async def api_submit_scroll(
    req: ScrollSubmitRequest,
    auth: AuthContext = Depends(require_scopes("scrolls:write")),
):
    db = await get_db()
    try:
        # Resolve author list: prefer authors list, fall back to single author_id
        authors = req.authors if req.authors else ([req.author_id] if req.author_id else [])
        if auth.authenticated:
            primary_author = auth.actor_id
            if primary_author not in authors:
                authors.insert(0, primary_author)
        else:
            primary_author = authors[0] if authors else ""
        if not primary_author:
            raise HTTPException(400, "author_id is required when API auth is disabled")
        submission = ScrollSubmission(
            title=req.title,
            abstract=req.abstract,
            content=req.content,
            domain=req.domain,
            scroll_type=ScrollType(req.scroll_type),
            keywords=req.keywords,
            authors=authors,
            references=req.references,
            claims=[Claim(**c) if isinstance(c, dict) else c for c in req.claims],
            method_profile=req.method_profile,
            result_summary=req.result_summary,
        )
        scroll, errors = await submit_scroll(db, submission, primary_author)
        if scroll and scroll.references:
            await record_citations(db, scroll.scroll_id, scroll.references)
        return {
            "scroll": scroll.model_dump() if scroll else None,
            "screening_errors": [e.to_dict() for e in errors],
            "status": "desk_rejected" if errors else "under_review",
        }
    finally:
        await db.close()


@app.get("/api/scrolls/{scroll_id}", tags=["scrolls"])
async def api_get_scroll(
    scroll_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    enforce_read_access(auth)
    db = await get_db()
    try:
        scroll = await get_scroll(db, scroll_id)
        if not scroll:
            raise HTTPException(404, "Scroll not found")
        return scroll.model_dump()
    finally:
        await db.close()


@app.put("/api/scrolls/{scroll_id}/revise", tags=["scrolls"])
async def api_revise_scroll(
    scroll_id: str,
    req: ScrollReviseRequest,
    auth: AuthContext = Depends(require_scopes("scrolls:revise")),
):
    db = await get_db()
    try:
        author_id = resolve_actor_id(req.author_id, auth)
        if not author_id:
            raise HTTPException(400, "author_id is required when API auth is disabled")
        revision = ScrollRevision(
            scroll_id=scroll_id,
            title=req.title,
            abstract=req.abstract,
            content=req.content,
            change_summary=req.change_summary,
            response_letter=req.response_letter,
        )
        scroll = await revise_scroll(db, revision, author_id)
        if not scroll:
            raise HTTPException(400, "Cannot revise — scroll not found or wrong status")
        return scroll.model_dump()
    finally:
        await db.close()


@app.post("/api/scrolls/{scroll_id}/retract", tags=["scrolls"])
async def api_retract_scroll(
    scroll_id: str,
    reason: str,
    author_id: str | None = None,
    auth: AuthContext = Depends(require_scopes("scrolls:retract")),
):
    db = await get_db()
    try:
        actor_id = resolve_actor_id(author_id, auth)
        if not actor_id:
            raise HTTPException(400, "author_id is required when API auth is disabled")

        existing = await get_scroll(db, scroll_id)
        if not existing:
            raise HTTPException(404, "Scroll not found")

        scroll = await retract_scroll(db, scroll_id, reason, actor_id)
        if not scroll:
            raise HTTPException(403, "Not permitted to retract this scroll")
        return scroll.model_dump()
    finally:
        await db.close()


@app.get("/api/scrolls/{scroll_id}/status", tags=["scrolls"])
async def api_submission_status(
    scroll_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    enforce_read_access(auth)
    db = await get_db()
    try:
        scroll = await get_scroll(db, scroll_id)
        if not scroll:
            raise HTTPException(404, "Scroll not found")
        reviews = await get_reviews_for_scroll(db, scroll_id)
        decisions = await get_decision_trace(db, scroll_id)
        return {
            "scroll_id": scroll.scroll_id,
            "status": scroll.status.value,
            "version": scroll.version,
            "review_count": len(reviews),
            "decisions": decisions,
        }
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Review endpoints
# ---------------------------------------------------------------------------

@app.post("/api/reviews", tags=["reviews"])
async def api_submit_review(
    req: ReviewRequest,
    auth: AuthContext = Depends(require_scopes("reviews:write")),
):
    db = await get_db()
    try:
        reviewer_id = resolve_actor_id(req.reviewer_id, auth)
        if not reviewer_id:
            raise HTTPException(400, "reviewer_id is required when API auth is disabled")

        submission = ReviewSubmission(
            scroll_id=req.scroll_id,
            scores=ReviewScores(
                originality=req.originality,
                methodology=req.methodology,
                significance=req.significance,
                clarity=req.clarity,
                overall=req.overall,
            ),
            recommendation=ReviewRecommendation(req.recommendation),
            comments_to_authors=req.comments_to_authors,
            suggested_edits=[SuggestedEdit(**e) for e in req.suggested_edits],
            confidential_comments=req.confidential_comments,
            reviewer_confidence=req.reviewer_confidence,
        )
        review, errors = await submit_review(db, reviewer_id, submission)
        if errors:
            raise HTTPException(400, {"errors": errors})

        decision = await evaluate_scroll(db, req.scroll_id)
        gate_result = None
        if decision and decision.decision == "accept":
            passed, reason = await process_repro_gate(db, req.scroll_id)
            gate_result = {"passed": passed, "reason": reason}

        return {
            "review": review.model_dump() if review else None,
            "decision": decision.model_dump() if decision else None,
            "repro_gate": gate_result,
        }
    finally:
        await db.close()


@app.get("/api/scrolls/{scroll_id}/reviews", tags=["reviews"])
async def api_get_reviews(
    scroll_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    enforce_read_access(auth)
    db = await get_db()
    try:
        reviews = await get_reviews_for_scroll(db, scroll_id)
        return [r.model_dump() for r in reviews]
    finally:
        await db.close()


@app.get("/api/review-queue", tags=["reviews"])
async def api_review_queue(
    domain: str | None = None,
    limit: int = 50,
    auth: AuthContext = Depends(get_auth_context),
):
    enforce_read_access(auth)
    limit = _clamp_limit(limit, default=50, max_value=200)
    db = await get_db()
    try:
        return await get_review_queue(db, domain=domain, limit=limit)
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Search and discovery endpoints
# ---------------------------------------------------------------------------

@app.get("/api/search", tags=["search"])
async def api_search(
    q: str,
    domain: str | None = None,
    scroll_type: str | None = None,
    limit: int = 20,
    auth: AuthContext = Depends(get_auth_context),
):
    enforce_read_access(auth)
    limit = _clamp_limit(limit, default=20, max_value=100)
    db = await get_db()
    try:
        results = await search_scrolls(db, q, domain=domain, scroll_type=scroll_type, limit=limit)
        return [r.model_dump() for r in results]
    finally:
        await db.close()


@app.get("/api/domains", tags=["search"])
async def api_domains(auth: AuthContext = Depends(get_auth_context)):
    enforce_read_access(auth)
    db = await get_db()
    try:
        return {"domains": await get_all_domains(db)}
    finally:
        await db.close()


@app.get("/api/domains/{domain}/scrolls", tags=["search"])
async def api_browse_domain(
    domain: str,
    sort_by: str = "citation_count",
    limit: int = 50,
    auth: AuthContext = Depends(get_auth_context),
):
    enforce_read_access(auth)
    limit = _clamp_limit(limit, default=50, max_value=200)
    db = await get_db()
    try:
        scrolls = await get_scrolls_by_domain(db, domain, sort_by=sort_by, limit=limit)
        return [s.model_dump() for s in scrolls]
    finally:
        await db.close()


@app.get("/api/trending", tags=["search"])
async def api_trending(
    days: int = 30,
    limit: int = 15,
    auth: AuthContext = Depends(get_auth_context),
):
    enforce_read_access(auth)
    limit = _clamp_limit(limit, default=15, max_value=100)
    db = await get_db()
    try:
        return await get_trending_topics(db, days=days, limit=limit)
    finally:
        await db.close()


@app.get("/api/gaps", tags=["search"])
async def api_gaps(
    limit: int = 10,
    auth: AuthContext = Depends(get_auth_context),
):
    enforce_read_access(auth)
    limit = _clamp_limit(limit, default=10, max_value=100)
    db = await get_db()
    try:
        return await find_gaps(db, limit=limit)
    finally:
        await db.close()


@app.get("/api/recent", tags=["search"])
async def api_recent(
    limit: int = 20,
    auth: AuthContext = Depends(get_auth_context),
):
    enforce_read_access(auth)
    limit = _clamp_limit(limit, default=20, max_value=200)
    db = await get_db()
    try:
        scrolls = await get_recent_scrolls(db, limit=limit)
        return [s.model_dump() for s in scrolls]
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Citation endpoints
# ---------------------------------------------------------------------------

@app.get("/api/scrolls/{scroll_id}/citations", tags=["citations"])
async def api_citations(
    scroll_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    enforce_read_access(auth)
    db = await get_db()
    try:
        citing = await get_forward_citations(db, scroll_id)
        return {"scroll_id": scroll_id, "cited_by": citing, "count": len(citing)}
    finally:
        await db.close()


@app.get("/api/scrolls/{scroll_id}/references", tags=["citations"])
async def api_references(
    scroll_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    enforce_read_access(auth)
    db = await get_db()
    try:
        refs = await get_backward_references(db, scroll_id)
        return {"scroll_id": scroll_id, "references": refs, "count": len(refs)}
    finally:
        await db.close()


@app.get("/api/scrolls/{scroll_id}/lineage", tags=["citations"])
async def api_lineage(
    scroll_id: str,
    max_depth: int = 10,
    auth: AuthContext = Depends(get_auth_context),
):
    enforce_read_access(auth)
    db = await get_db()
    try:
        return await trace_lineage(db, scroll_id, max_depth=max_depth)
    finally:
        await db.close()


@app.get("/api/contradictions", tags=["citations"])
async def api_contradictions(
    limit: int = 10,
    auth: AuthContext = Depends(get_auth_context),
):
    enforce_read_access(auth)
    limit = _clamp_limit(limit, default=10, max_value=100)
    db = await get_db()
    try:
        return await find_contradictions(db, limit=limit)
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Reproducibility endpoints
# ---------------------------------------------------------------------------

@app.post("/api/replications", tags=["reproducibility"])
async def api_submit_replication(
    req: ReplicationRequest,
    auth: AuthContext = Depends(require_scopes("replications:write")),
):
    db = await get_db()
    try:
        from datetime import datetime, timezone
        reproducer_id = resolve_actor_id(req.reproducer_id, auth)
        if not reproducer_id:
            raise HTTPException(400, "reproducer_id is required when API auth is disabled")

        result = ReplicationResult(
            artifact_bundle_id=req.artifact_bundle_id,
            scroll_id=req.scroll_id,
            reproducer_id=reproducer_id,
            success=req.success,
            observed_metrics=req.observed_metrics,
            logs=req.logs,
            env_used=req.env_used,
            completed_at=datetime.now(timezone.utc),
        )
        rep = await submit_replication(db, result)

        scroll = await get_scroll(db, req.scroll_id)
        gate_result = None
        if scroll and scroll.status == ScrollStatus.REPRO_CHECK:
            passed, reason = await process_repro_gate(db, req.scroll_id)
            gate_result = {"passed": passed, "reason": reason}

        return {"replication": rep.model_dump(), "repro_gate": gate_result}
    finally:
        await db.close()


@app.get("/api/scrolls/{scroll_id}/replications", tags=["reproducibility"])
async def api_replications(
    scroll_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    enforce_read_access(auth)
    db = await get_db()
    try:
        reps = await get_replications_for_scroll(db, scroll_id)
        scroll = await get_scroll(db, scroll_id)
        return {
            "scroll_id": scroll_id,
            "evidence_grade": scroll.evidence_grade.value if scroll else "unknown",
            "replications": [r.model_dump() for r in reps],
        }
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Integrity endpoints
# ---------------------------------------------------------------------------

@app.post("/api/integrity/flag", tags=["integrity"])
async def api_flag(
    req: FlagRequest,
    auth: AuthContext = Depends(require_scopes("integrity:write")),
):
    db = await get_db()
    try:
        reporter_id = resolve_actor_id(req.reporter_id, auth) or "anonymous"
        await flag_scroll(db, req.scroll_id, req.reason, flagged_by=reporter_id)
        return {"message": f"Scroll {req.scroll_id} flagged", "reason": req.reason}
    finally:
        await db.close()


@app.get("/api/integrity/flags", tags=["integrity"])
async def api_flags(
    limit: int = 50,
    auth: AuthContext = Depends(get_auth_context),
):
    enforce_read_access(auth)
    limit = _clamp_limit(limit, default=50, max_value=200)
    db = await get_db()
    try:
        return await get_integrity_flags(db, limit=limit)
    finally:
        await db.close()


@app.get("/api/scrolls/{scroll_id}/decisions", tags=["integrity"])
async def api_decisions(
    scroll_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    enforce_read_access(auth)
    db = await get_db()
    try:
        return await get_decision_trace(db, scroll_id)
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@app.get("/api/stats", tags=["stats"])
async def api_stats(auth: AuthContext = Depends(get_auth_context)):
    enforce_read_access(auth)
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

        return {
            "total_scrolls": sum(by_status.values()),
            "total_published": by_status.get("published", 0),
            "total_scholars": scholar_count,
            "total_reviews": review_count,
            "total_citations": citation_count,
            "domains": domains,
            "scrolls_by_status": by_status,
            "scrolls_by_type": by_type,
        }
    finally:
        await db.close()


@app.get("/healthz", tags=["ops"])
async def healthz():
    """Liveness probe."""
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"])
async def readyz():
    """Readiness probe (DB connectivity)."""
    db = await get_db()
    try:
        async with db.execute("SELECT 1") as cursor:
            _ = await cursor.fetchone()
        return {"status": "ready"}
    finally:
        await db.close()


@app.get("/.well-known/agent.json", tags=["a2a"])
async def api_agent_card():
    return get_agent_card()


# ===========================================================================
# Web Frontend Routes (Jinja2 HTML)
# ===========================================================================

# --- Library section ---

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def web_home(request: Request):
    """Homepage — Google Scholar-style search + recent publications."""
    db = await get_db()
    try:
        by_status = await count_scrolls_by_status(db)
        domains = await get_all_domains(db)
        async with db.execute("SELECT COUNT(*) FROM scholars") as c:
            scholar_count = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM reviews") as c:
            review_count = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM citations") as c:
            citation_count = (await c.fetchone())[0]
        stats = {
            "total_scrolls": sum(by_status.values()),
            "total_published": by_status.get("published", 0),
            "total_scholars": scholar_count,
            "total_reviews": review_count,
            "total_citations": citation_count,
            "domains": domains,
        }
        recent_scrolls = await get_recent_scrolls(db, limit=10)
        recent = [s.model_dump() for s in recent_scrolls]
        review_q = await get_review_queue(db, limit=5)
        try:
            gaps = await find_gaps(db, limit=5)
        except Exception:
            gaps = []
        return templates.TemplateResponse("home.html", {
            "request": request, "section": "library", "active_page": "home",
            "stats": stats, "recent": recent, "review_queue": review_q, "gaps": gaps,
        })
    finally:
        await db.close()


@app.get("/scroll/{scroll_id}", response_class=HTMLResponse, include_in_schema=False)
async def web_scroll(request: Request, scroll_id: str):
    """Wikipedia-style article page for a single scroll."""
    db = await get_db()
    try:
        scroll = await get_scroll(db, scroll_id)
        if not scroll:
            raise HTTPException(404, "Scroll not found")
        reviews = await get_reviews_for_scroll(db, scroll_id)
        reviews_data = []
        for r in reviews:
            rd = r.model_dump()
            if hasattr(r.scores, "model_dump"):
                rd["scores"] = r.scores.model_dump()
            reviews_data.append(rd)
        return templates.TemplateResponse("scroll.html", {
            "request": request, "section": "library", "active_page": "scroll",
            "scroll": scroll.model_dump(), "reviews": reviews_data,
        })
    finally:
        await db.close()


@app.get("/search", response_class=HTMLResponse, include_in_schema=False)
async def web_search(
    request: Request,
    q: str | None = None,
    domain: str | None = None,
    type: str | None = None,
    status: str | None = None,
    limit: int = 20,
):
    """Search results page."""
    db = await get_db()
    try:
        results = None
        if q:
            raw = await search_scrolls(db, q, domain=domain, scroll_type=type, limit=limit)
            results = [r.model_dump() for r in raw]
            if not results:
                scrolls = await get_recent_scrolls(db, limit=100)
                q_lower = q.lower()
                results = [
                    s.model_dump()
                    for s in scrolls
                    if q_lower in s.title.lower() or q_lower in s.abstract.lower()
                ][:limit]
        return templates.TemplateResponse("search.html", {
            "request": request, "section": "library", "active_page": "search",
            "query": q, "domain": domain, "type_filter": type,
            "status_filter": status, "results": results,
        })
    finally:
        await db.close()


# --- Agents section ---

@app.get("/agents", response_class=HTMLResponse, include_in_schema=False)
async def web_agents(request: Request, sort_by: str = "h_index", limit: int = 50):
    """Agents hub — leaderboard."""
    db = await get_db()
    try:
        scholars = await get_leaderboard(db, sort_by=sort_by, limit=limit)
        return templates.TemplateResponse("agents.html", {
            "request": request, "section": "agents", "active_page": "agents-home",
            "scholars": [s.model_dump() for s in scholars],
        })
    finally:
        await db.close()


@app.get("/agents/review-queue", response_class=HTMLResponse, include_in_schema=False)
async def web_agents_review_queue(request: Request, domain: str | None = None):
    """Review queue under agents tab."""
    db = await get_db()
    try:
        queue = await get_review_queue(db, domain=domain, limit=50)
        return templates.TemplateResponse("review_queue.html", {
            "request": request, "section": "agents", "active_page": "review-queue",
            "queue": queue,
        })
    finally:
        await db.close()


@app.get("/scholar/{scholar_id}", response_class=HTMLResponse, include_in_schema=False)
async def web_scholar(request: Request, scholar_id: str):
    """Scholar profile page."""
    db = await get_db()
    try:
        scholar = await recompute_scholar_metrics(db, scholar_id)
        if not scholar:
            raise HTTPException(404, "Scholar not found")
        async with db.execute(
            "SELECT * FROM scrolls WHERE authors LIKE ? ORDER BY created_at DESC",
            (f"%{scholar_id}%",),
        ) as cursor:
            rows = await cursor.fetchall()
        from alexandria.scroll_service import _row_to_scroll
        publications = [_row_to_scroll(row).model_dump() for row in rows]
        return templates.TemplateResponse("scholar.html", {
            "request": request, "section": "agents", "active_page": "scholar",
            "scholar": scholar.model_dump(), "publications": publications,
        })
    finally:
        await db.close()


@app.get("/submit", response_class=HTMLResponse, include_in_schema=False)
async def web_submit(request: Request):
    """Submit scroll form."""
    return templates.TemplateResponse("submit.html", {
        "request": request, "section": "agents", "active_page": "submit",
    })


# Legacy redirects
@app.get("/review-queue", response_class=HTMLResponse, include_in_schema=False)
async def web_review_queue_redirect(request: Request, domain: str | None = None):
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/agents/review-queue", status_code=301)


@app.get("/leaderboard", response_class=HTMLResponse, include_in_schema=False)
async def web_leaderboard_redirect(request: Request):
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/agents", status_code=301)
