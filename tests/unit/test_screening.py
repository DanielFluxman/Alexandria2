"""Unit tests for editorial screening (desk check) rules."""

import pytest
from alexandria.models import Claim, ScrollSubmission, ScrollType
from alexandria.scroll_service import screen_submission


class TestEditorialScreening:
    def test_valid_paper_passes(self):
        sub = ScrollSubmission(
            title="A Study on Caching",
            abstract="A" * 100,
            content="B" * 300,
            domain="software-engineering",
            authors=["scholar-1"],
        )
        errors = screen_submission(sub)
        assert len(errors) == 0

    def test_missing_title_fails(self):
        sub = ScrollSubmission(
            title="",
            abstract="A" * 100,
            content="B" * 300,
            domain="software-engineering",
            authors=["scholar-1"],
        )
        errors = screen_submission(sub)
        assert any(e.rule == "title_required" for e in errors)

    def test_short_abstract_fails(self):
        sub = ScrollSubmission(
            title="Test",
            abstract="Too short",
            content="B" * 300,
            domain="software-engineering",
            authors=["scholar-1"],
        )
        errors = screen_submission(sub)
        assert any(e.rule == "abstract_too_short" for e in errors)

    def test_short_content_fails(self):
        sub = ScrollSubmission(
            title="Test",
            abstract="A" * 100,
            content="Short",
            domain="software-engineering",
            authors=["scholar-1"],
        )
        errors = screen_submission(sub)
        assert any(e.rule == "content_too_short" for e in errors)

    def test_no_authors_fails(self):
        sub = ScrollSubmission(
            title="Test",
            abstract="A" * 100,
            content="B" * 300,
            domain="software-engineering",
            authors=[],
        )
        errors = screen_submission(sub)
        assert any(e.rule == "authors_required" for e in errors)

    def test_no_domain_fails(self):
        sub = ScrollSubmission(
            title="Test",
            abstract="A" * 100,
            content="B" * 300,
            domain="",
            authors=["scholar-1"],
        )
        errors = screen_submission(sub)
        assert any(e.rule == "domain_required" for e in errors)

    def test_hypothesis_needs_claims(self):
        sub = ScrollSubmission(
            title="Hypothesis: X > Y",
            abstract="A" * 100,
            content="B" * 300,
            domain="ai-theory",
            scroll_type=ScrollType.HYPOTHESIS,
            authors=["scholar-1"],
            claims=[],
        )
        errors = screen_submission(sub)
        assert any(e.rule == "hypothesis_needs_claims" for e in errors)

    def test_hypothesis_with_claims_passes(self):
        sub = ScrollSubmission(
            title="Hypothesis: X > Y",
            abstract="A" * 100,
            content="B" * 300,
            domain="ai-theory",
            scroll_type=ScrollType.HYPOTHESIS,
            authors=["scholar-1"],
            claims=[Claim(statement="X outperforms Y", evidence_type="empirical")],
        )
        errors = screen_submission(sub)
        assert len(errors) == 0

    def test_meta_analysis_needs_references(self):
        sub = ScrollSubmission(
            title="Meta-Analysis",
            abstract="A" * 100,
            content="B" * 300,
            domain="ai-theory",
            scroll_type=ScrollType.META_ANALYSIS,
            authors=["scholar-1"],
            references=[],
        )
        errors = screen_submission(sub)
        assert any(e.rule == "meta_analysis_needs_references" for e in errors)

    def test_rebuttal_needs_target(self):
        sub = ScrollSubmission(
            title="Rebuttal",
            abstract="A" * 100,
            content="B" * 300,
            domain="ai-theory",
            scroll_type=ScrollType.REBUTTAL,
            authors=["scholar-1"],
            references=[],
        )
        errors = screen_submission(sub)
        assert any(e.rule == "rebuttal_needs_target" for e in errors)

    def test_multiple_errors(self):
        sub = ScrollSubmission(
            title="",
            abstract="Short",
            content="X",
            domain="",
            authors=[],
        )
        errors = screen_submission(sub)
        assert len(errors) >= 4  # title, abstract, content, authors, domain
