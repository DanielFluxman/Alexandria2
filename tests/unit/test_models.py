"""Unit tests for domain models."""

import pytest
from alexandria.models import (
    Claim,
    DecisionRecord,
    EvidenceGrade,
    PolicyRuleEvaluation,
    Review,
    ReviewRecommendation,
    ReviewScores,
    Scholar,
    ScholarCreate,
    Scroll,
    ScrollStatus,
    ScrollSubmission,
    ScrollType,
    SuggestedEdit,
    TrustTier,
)


class TestScrollModels:
    def test_scroll_defaults(self):
        scroll = Scroll(title="Test Paper")
        assert scroll.title == "Test Paper"
        assert scroll.scroll_type == ScrollType.PAPER
        assert scroll.status == ScrollStatus.SUBMITTED
        assert scroll.version == 1
        assert scroll.citation_count == 0
        assert scroll.evidence_grade == EvidenceGrade.UNGRADED

    def test_scroll_submission_validation(self):
        sub = ScrollSubmission(
            title="Test",
            abstract="A" * 100,
            content="B" * 300,
            domain="ai-theory",
            authors=["scholar-1"],
        )
        assert sub.scroll_type == ScrollType.PAPER
        assert len(sub.abstract) == 100

    def test_scroll_types(self):
        for st in ScrollType:
            scroll = Scroll(title="T", scroll_type=st)
            assert scroll.scroll_type == st

    def test_scroll_status_values(self):
        expected = {
            "submitted", "screened", "desk_rejected", "under_review",
            "revisions_required", "repro_check", "accepted", "published",
            "flagged", "retracted", "superseded", "rejected",
        }
        actual = {s.value for s in ScrollStatus}
        assert actual == expected


class TestReviewModels:
    def test_review_scores_mean(self):
        scores = ReviewScores(
            originality=8,
            methodology=6,
            significance=7,
            clarity=9,
            overall=7,
        )
        assert scores.mean == pytest.approx(7.4)

    def test_review_scores_validation(self):
        with pytest.raises(Exception):
            ReviewScores(originality=0, methodology=6, significance=7, clarity=9, overall=7)
        with pytest.raises(Exception):
            ReviewScores(originality=11, methodology=6, significance=7, clarity=9, overall=7)

    def test_review_creation(self):
        review = Review(
            scroll_id="AX-2026-00001",
            reviewer_id="scholar-1",
            scores=ReviewScores(originality=8, methodology=7, significance=6, clarity=8, overall=7),
            recommendation=ReviewRecommendation.MINOR_REVISIONS,
            comments_to_authors="Good work, minor issues.",
        )
        assert review.review_round == 1
        assert review.recommendation == ReviewRecommendation.MINOR_REVISIONS

    def test_suggested_edit(self):
        edit = SuggestedEdit(
            section="Methodology",
            original_text="We used method A",
            proposed_text="We applied method A with modification B",
            rationale="Method A alone is insufficient",
        )
        assert edit.section == "Methodology"


class TestScholarModels:
    def test_scholar_defaults(self):
        scholar = Scholar(name="TestBot")
        assert scholar.trust_tier == TrustTier.NEW
        assert scholar.h_index == 0
        assert scholar.reputation_score == 0.0

    def test_scholar_create(self):
        create = ScholarCreate(name="Claude", affiliation="Anthropic", bio="An AI assistant")
        assert create.name == "Claude"


class TestClaimModel:
    def test_claim(self):
        claim = Claim(statement="X outperforms Y on benchmark Z", evidence_type="empirical")
        assert claim.falsifiable is True
        assert claim.evidence_type == "empirical"


class TestDecisionRecord:
    def test_decision_record(self):
        rule = PolicyRuleEvaluation(
            rule_name="score_threshold",
            input_data={"avg_overall": 7.5, "threshold": 6.0},
            result=True,
            explanation="Met: 7.5 >= 6.0",
        )
        record = DecisionRecord(
            scroll_id="AX-2026-00001",
            decision="accept",
            rule_evaluations=[rule],
            explanation="All criteria met",
        )
        assert record.decision == "accept"
        assert len(record.rule_evaluations) == 1
        assert record.rule_evaluations[0].result is True
