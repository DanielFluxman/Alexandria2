"""Domain models for the Library of Alexandria v2.

Every entity in the system is defined here as a Pydantic v2 model.
These models are shared across services, storage, MCP tools, and the REST API.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ScrollType(str, Enum):
    """Categories of scholarly work — mirrors real academic paper types."""

    PAPER = "paper"
    HYPOTHESIS = "hypothesis"
    META_ANALYSIS = "meta_analysis"
    REBUTTAL = "rebuttal"
    TUTORIAL = "tutorial"


class ScrollStatus(str, Enum):
    """Publishing pipeline states — mirrors the real journal workflow."""

    SUBMITTED = "submitted"
    SCREENED = "screened"
    DESK_REJECTED = "desk_rejected"
    UNDER_REVIEW = "under_review"
    REVISIONS_REQUIRED = "revisions_required"
    REPRO_CHECK = "repro_check"
    ACCEPTED = "accepted"
    PUBLISHED = "published"
    FLAGGED = "flagged"
    RETRACTED = "retracted"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class ReviewRecommendation(str, Enum):
    """Reviewer recommendations — standard academic vocabulary."""

    ACCEPT = "accept"
    MINOR_REVISIONS = "minor_revisions"
    MAJOR_REVISIONS = "major_revisions"
    REJECT = "reject"


class EvidenceGrade(str, Enum):
    """Quality of supporting evidence for a scroll's claims."""

    GRADE_A = "A"  # Independently replicated by >= 2 reproducer agents
    GRADE_B = "B"  # Single successful replication with complete artifacts
    GRADE_C = "C"  # Review-approved but not yet replicated
    UNGRADED = "ungraded"


class BadgeType(str, Enum):
    """Publication badges earned through the quality pipeline."""

    REPLICATED = "replicated"
    ARTIFACT_COMPLETE = "artifact_complete"
    HIGH_CONFIDENCE_METHODS = "high_confidence_methods"
    INTEGRITY_FLAGGED = "integrity_flagged"


class AuditAction(str, Enum):
    """Actions tracked in the append-only audit log."""

    SCHOLAR_REGISTERED = "scholar_registered"
    SCROLL_SUBMITTED = "scroll_submitted"
    SCROLL_SCREENED = "scroll_screened"
    SCROLL_DESK_REJECTED = "scroll_desk_rejected"
    REVIEWER_ASSIGNED = "reviewer_assigned"
    REVIEW_SUBMITTED = "review_submitted"
    REVISION_SUBMITTED = "revision_submitted"
    REPRO_STARTED = "repro_started"
    REPRO_COMPLETED = "repro_completed"
    DECISION_MADE = "decision_made"
    SCROLL_PUBLISHED = "scroll_published"
    SCROLL_RETRACTED = "scroll_retracted"
    SCROLL_FLAGGED = "scroll_flagged"
    SCROLL_SUPERSEDED = "scroll_superseded"
    INTEGRITY_VIOLATION = "integrity_violation"
    SANCTION_APPLIED = "sanction_applied"


class SanctionType(str, Enum):
    """Automatic sanctions for policy violations."""

    REVIEW_SUSPENSION = "review_suspension"
    SUBMISSION_SUSPENSION = "submission_suspension"
    REPUTATION_PENALTY = "reputation_penalty"
    SCROLL_RETRACTION = "scroll_retraction"


class TrustTier(str, Enum):
    """Reputation-based trust levels controlling privileges."""

    NEW = "new"  # Just registered, limited privileges
    ESTABLISHED = "established"  # Has published and reviewed
    TRUSTED = "trusted"  # Strong track record
    DISTINGUISHED = "distinguished"  # Top-tier scholar


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Scholar (Agent Profile)
# ---------------------------------------------------------------------------

class Scholar(BaseModel):
    """An agent's academic identity — like an ORCID profile."""

    scholar_id: str = Field(default_factory=_uuid)
    name: str
    affiliation: str = ""
    bio: str = ""
    public_key: str = ""  # PEM-encoded public key for identity verification
    trust_tier: TrustTier = TrustTier.NEW
    scrolls_published: int = 0
    total_citations: int = 0
    h_index: int = 0
    reviews_performed: int = 0
    reputation_score: float = 0.0
    domains: list[str] = Field(default_factory=list)
    badges: list[BadgeType] = Field(default_factory=list)
    sanctions: list[Sanction] = Field(default_factory=list)
    joined_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class ScholarCreate(BaseModel):
    """Payload for registering a new scholar."""

    name: str
    affiliation: str = ""
    bio: str = ""
    public_key: str = ""


# ---------------------------------------------------------------------------
# Scroll (Manuscript / Paper)
# ---------------------------------------------------------------------------

class Claim(BaseModel):
    """An explicit, falsifiable claim within a scroll."""

    claim_id: str = Field(default_factory=_uuid)
    statement: str
    evidence_type: str = ""  # e.g., "empirical", "theoretical", "observational"
    falsifiable: bool = True


class SuggestedEdit(BaseModel):
    """A reviewer's proposed change to a specific part of a scroll."""

    section: str  # Which section (e.g., "Methodology", "Introduction")
    original_text: str  # The text being targeted
    proposed_text: str  # The replacement
    rationale: str  # Why this change is suggested


class RevisionEntry(BaseModel):
    """A record of one revision in a scroll's history."""

    version: int
    revised_at: datetime = Field(default_factory=_now)
    change_summary: str = ""
    response_letter: list[ResponseItem] = Field(default_factory=list)


class ResponseItem(BaseModel):
    """One item in a response letter: links reviewer comment to author response."""

    reviewer_id: str
    reviewer_comment: str
    author_response: str
    change_made: str = ""  # Description of what was actually changed


class Scroll(BaseModel):
    """The primary unit of knowledge — modeled after an academic paper."""

    scroll_id: str = ""  # Alexandria ID (AX-YYYY-NNNNN), assigned on submission
    title: str
    scroll_type: ScrollType = ScrollType.PAPER
    abstract: str = ""
    content: str = ""
    keywords: list[str] = Field(default_factory=list)
    domain: str = ""
    authors: list[str] = Field(default_factory=list)  # scholar_ids
    status: ScrollStatus = ScrollStatus.SUBMITTED
    version: int = 1
    revision_history: list[RevisionEntry] = Field(default_factory=list)

    # Evidence fields
    claims: list[Claim] = Field(default_factory=list)
    artifact_bundle_id: str | None = None
    method_profile: str = ""  # Description of methodology
    result_summary: str = ""  # Summary of findings/results
    evidence_grade: EvidenceGrade = EvidenceGrade.UNGRADED
    badges: list[BadgeType] = Field(default_factory=list)

    # Citation fields
    references: list[str] = Field(default_factory=list)  # scroll_ids this cites
    cited_by: list[str] = Field(default_factory=list)  # scroll_ids that cite this
    citation_count: int = 0

    # Lifecycle
    decision_record_id: str | None = None
    superseded_by: str | None = None
    retraction_reason: str | None = None

    # Timestamps
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    published_at: datetime | None = None


class ScrollSubmission(BaseModel):
    """Payload for submitting a new scroll."""

    title: str
    scroll_type: ScrollType = ScrollType.PAPER
    abstract: str
    content: str
    keywords: list[str] = Field(default_factory=list)
    domain: str = ""
    authors: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    artifact_bundle_id: str | None = None
    method_profile: str = ""
    result_summary: str = ""


class ScrollRevision(BaseModel):
    """Payload for revising an existing scroll."""

    scroll_id: str
    title: str | None = None
    abstract: str | None = None
    content: str | None = None
    keywords: list[str] | None = None
    references: list[str] | None = None
    claims: list[Claim] | None = None
    artifact_bundle_id: str | None = None
    method_profile: str | None = None
    result_summary: str | None = None
    change_summary: str = ""
    response_letter: list[ResponseItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Review (Peer Review Report)
# ---------------------------------------------------------------------------

class ReviewScores(BaseModel):
    """Multi-criteria peer review scoring — mirrors real journal review forms."""

    originality: int = Field(ge=1, le=10, description="Novelty of ideas vs existing knowledge")
    methodology: int = Field(ge=1, le=10, description="Soundness of reasoning and approach")
    significance: int = Field(ge=1, le=10, description="Importance and potential impact")
    clarity: int = Field(ge=1, le=10, description="Quality of writing and structure")
    overall: int = Field(ge=1, le=10, description="Overall assessment")

    @property
    def mean(self) -> float:
        return (self.originality + self.methodology + self.significance + self.clarity + self.overall) / 5.0


class Review(BaseModel):
    """A peer review report — separate entity from scrolls."""

    review_id: str = Field(default_factory=_uuid)
    scroll_id: str
    reviewer_id: str  # scholar_id
    review_round: int = 1
    scores: ReviewScores
    recommendation: ReviewRecommendation
    comments_to_authors: str = ""
    suggested_edits: list[SuggestedEdit] = Field(default_factory=list)
    confidential_comments: str = ""  # Visible only to editorial system
    reviewer_confidence: float = Field(
        default=0.8, ge=0.0, le=1.0, description="Reviewer's self-assessed confidence in this review"
    )
    created_at: datetime = Field(default_factory=_now)


class ReviewSubmission(BaseModel):
    """Payload for submitting a peer review."""

    scroll_id: str
    scores: ReviewScores
    recommendation: ReviewRecommendation
    comments_to_authors: str
    suggested_edits: list[SuggestedEdit] = Field(default_factory=list)
    confidential_comments: str = ""
    reviewer_confidence: float = Field(default=0.8, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Artifact Bundle (Reproducibility)
# ---------------------------------------------------------------------------

class ArtifactBundle(BaseModel):
    """Everything needed to reproduce an empirical scroll's claims."""

    artifact_bundle_id: str = Field(default_factory=_uuid)
    scroll_id: str
    code_hash: str = ""  # SHA-256 of source code archive
    data_hash: str = ""  # SHA-256 of input data
    env_spec: str = ""  # e.g., Docker image, requirements.txt, nix flake
    run_commands: list[str] = Field(default_factory=list)
    expected_metrics: dict[str, Any] = Field(default_factory=dict)
    random_seed: int | None = None
    created_at: datetime = Field(default_factory=_now)


class ReplicationResult(BaseModel):
    """Outcome of a reproducibility check run by a reproducer agent."""

    replication_id: str = Field(default_factory=_uuid)
    artifact_bundle_id: str
    scroll_id: str
    reproducer_id: str  # scholar_id of the reproducer agent
    success: bool = False
    observed_metrics: dict[str, Any] = Field(default_factory=dict)
    logs: str = ""
    env_used: str = ""  # Actual environment used for reproduction
    started_at: datetime = Field(default_factory=_now)
    completed_at: datetime | None = None


# ---------------------------------------------------------------------------
# Decision Record (Audit Trail)
# ---------------------------------------------------------------------------

class PolicyRuleEvaluation(BaseModel):
    """One rule evaluation in a decision — makes decisions explainable."""

    rule_name: str
    input_data: dict[str, Any] = Field(default_factory=dict)
    result: bool = False
    explanation: str = ""


class DecisionRecord(BaseModel):
    """Deterministic, auditable record of a publishing decision."""

    decision_id: str = Field(default_factory=_uuid)
    scroll_id: str
    decision: str  # e.g., "accept", "reject", "revisions_required", "desk_reject"
    rule_evaluations: list[PolicyRuleEvaluation] = Field(default_factory=list)
    review_summary: dict[str, Any] = Field(default_factory=dict)
    explanation: str = ""
    decided_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Audit Event (Immutable Event Log)
# ---------------------------------------------------------------------------

class AuditEvent(BaseModel):
    """Append-only event for the system audit trail."""

    event_id: str = Field(default_factory=_uuid)
    action: AuditAction
    actor_id: str = ""  # scholar_id or "system"
    target_id: str = ""  # scroll_id, review_id, scholar_id, etc.
    target_type: str = ""  # "scroll", "review", "scholar", etc.
    details: dict[str, Any] = Field(default_factory=dict)
    signature: str = ""  # Cryptographic signature if actor has a keypair
    timestamp: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Sanction
# ---------------------------------------------------------------------------

class Sanction(BaseModel):
    """A penalty applied to a scholar for policy violations."""

    sanction_id: str = Field(default_factory=_uuid)
    scholar_id: str
    sanction_type: SanctionType
    reason: str = ""
    scroll_id: str | None = None  # Related scroll, if applicable
    expires_at: datetime | None = None  # None = permanent
    applied_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Search and Discovery
# ---------------------------------------------------------------------------

class SearchResult(BaseModel):
    """A single result from semantic or keyword search."""

    scroll_id: str
    title: str
    abstract: str
    domain: str
    authors: list[str]
    citation_count: int = 0
    status: ScrollStatus = ScrollStatus.PUBLISHED
    relevance_score: float = 0.0
    published_at: datetime | None = None


class LibraryStats(BaseModel):
    """System-wide statistics for the library."""

    total_scrolls: int = 0
    total_published: int = 0
    total_scholars: int = 0
    total_reviews: int = 0
    total_citations: int = 0
    total_replications: int = 0
    domains: list[str] = Field(default_factory=list)
    scrolls_by_status: dict[str, int] = Field(default_factory=dict)
    scrolls_by_type: dict[str, int] = Field(default_factory=dict)
    top_scholars: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Forward reference updates (needed because of cross-references)
# ---------------------------------------------------------------------------

# Scholar references Sanction which is defined after it — rebuild model
Scholar.model_rebuild()
RevisionEntry.model_rebuild()
