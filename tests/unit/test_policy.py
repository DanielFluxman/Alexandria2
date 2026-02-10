"""Unit tests for policy engine decision rules."""

import pytest
from alexandria.policy_engine import (
    _rule_min_reviews,
    _rule_no_reject_majority,
    _rule_no_unresolved_critical_flags,
    _rule_revision_limit,
    _rule_revisions_needed,
    _rule_score_threshold,
)


class TestPolicyRules:
    def test_min_reviews_normal_pass(self):
        result = _rule_min_reviews(2, "software-engineering")
        assert result.result is True

    def test_min_reviews_normal_fail(self):
        result = _rule_min_reviews(1, "software-engineering")
        assert result.result is False

    def test_min_reviews_high_impact_pass(self):
        result = _rule_min_reviews(3, "ai-theory")
        assert result.result is True

    def test_min_reviews_high_impact_fail(self):
        result = _rule_min_reviews(2, "ai-theory")
        assert result.result is False

    def test_score_threshold_pass(self):
        result = _rule_score_threshold(7.5)
        assert result.result is True

    def test_score_threshold_fail(self):
        result = _rule_score_threshold(4.0)
        assert result.result is False

    def test_score_threshold_exact(self):
        result = _rule_score_threshold(6.0)
        assert result.result is True

    def test_no_reject_majority_pass(self):
        result = _rule_no_reject_majority(["accept", "minor_revisions", "accept"])
        assert result.result is True

    def test_no_reject_majority_fail(self):
        result = _rule_no_reject_majority(["reject", "reject", "accept"])
        assert result.result is False

    def test_no_reject_majority_empty(self):
        result = _rule_no_reject_majority([])
        assert result.result is True

    def test_critical_flags_pass(self):
        result = _rule_no_unresolved_critical_flags([
            {"recommendation": "accept", "reviewer_confidence": 0.9},
        ])
        assert result.result is True

    def test_critical_flags_fail(self):
        result = _rule_no_unresolved_critical_flags([
            {"recommendation": "reject", "reviewer_confidence": 0.9},
        ])
        assert result.result is False

    def test_critical_flags_low_confidence_ok(self):
        result = _rule_no_unresolved_critical_flags([
            {"recommendation": "reject", "reviewer_confidence": 0.5},
        ])
        assert result.result is True

    def test_revision_limit_within(self):
        result = _rule_revision_limit(1)
        assert result.result is True

    def test_revision_limit_exceeded(self):
        result = _rule_revision_limit(5)  # default max is 3 rounds
        assert result.result is False

    def test_revisions_needed_yes(self):
        result = _rule_revisions_needed(["minor_revisions", "accept"])
        assert result.result is True

    def test_revisions_needed_no(self):
        result = _rule_revisions_needed(["accept", "accept"])
        assert result.result is False
