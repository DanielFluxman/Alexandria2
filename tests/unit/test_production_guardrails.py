"""Production guardrail tests: auth, permissions, and workflow safety checks."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from alexandria.api import app
from alexandria.auth import reload_api_key_cache
from alexandria.config import settings
from alexandria.database import SCHEMA_SQL
from alexandria.models import (
    ReviewRecommendation,
    ReviewScores,
    ReviewSubmission,
    ScholarCreate,
    ScrollRevision,
    ScrollStatus,
    ScrollSubmission,
)
from alexandria.review_service import submit_review
from alexandria.scholar_service import register_scholar
from alexandria.scroll_service import retract_scroll, revise_scroll, submit_scroll


async def _memory_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA_SQL)
    await db.execute("PRAGMA foreign_keys = ON;")
    await db.commit()
    return db


@pytest.mark.asyncio
async def test_submit_scroll_rejects_unknown_references():
    db = await _memory_db()
    try:
        author = await register_scholar(db, ScholarCreate(name="Author"))
        good_sub = ScrollSubmission(
            title="Known-good",
            abstract="A" * 80,
            content="B" * 250,
            domain="software-engineering",
            authors=[author.scholar_id],
        )
        scroll_ok, errors_ok = await submit_scroll(db, good_sub, author.scholar_id)
        assert scroll_ok is not None
        assert errors_ok == []
        assert scroll_ok.status == ScrollStatus.UNDER_REVIEW

        bad_sub = ScrollSubmission(
            title="Bad references",
            abstract="A" * 80,
            content="B" * 250,
            domain="software-engineering",
            authors=[author.scholar_id],
            references=["AX-2099-99999"],
        )
        scroll_bad, errors_bad = await submit_scroll(db, bad_sub, author.scholar_id)
        assert scroll_bad is not None
        assert any(e.rule == "invalid_references" for e in errors_bad)
        assert scroll_bad.status == ScrollStatus.DESK_REJECTED
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_only_author_can_revise_or_retract():
    db = await _memory_db()
    try:
        author = await register_scholar(db, ScholarCreate(name="Author"))
        outsider = await register_scholar(db, ScholarCreate(name="Outsider"))

        sub = ScrollSubmission(
            title="Ownership test",
            abstract="A" * 80,
            content="B" * 250,
            domain="systems",
            authors=[author.scholar_id],
        )
        scroll, errors = await submit_scroll(db, sub, author.scholar_id)
        assert scroll is not None and not errors

        await db.execute(
            "UPDATE scrolls SET status = ? WHERE scroll_id = ?",
            (ScrollStatus.REVISIONS_REQUIRED.value, scroll.scroll_id),
        )
        await db.commit()

        revision = ScrollRevision(
            scroll_id=scroll.scroll_id,
            content="Revised content",
            change_summary="Address reviewer comments",
        )
        denied = await revise_scroll(db, revision, outsider.scholar_id)
        assert denied is None

        allowed = await revise_scroll(db, revision, author.scholar_id)
        assert allowed is not None
        assert allowed.version == 2

        await db.execute(
            "UPDATE scrolls SET status = ? WHERE scroll_id = ?",
            (ScrollStatus.PUBLISHED.value, scroll.scroll_id),
        )
        await db.commit()

        denied_retract = await retract_scroll(
            db,
            scroll.scroll_id,
            "malicious attempt",
            outsider.scholar_id,
        )
        assert denied_retract is None

        allowed_retract = await retract_scroll(
            db,
            scroll.scroll_id,
            "author-requested correction",
            author.scholar_id,
        )
        assert allowed_retract is not None
        assert allowed_retract.status == ScrollStatus.RETRACTED
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reviewer_must_exist_and_round_logic_allows_rereview():
    db = await _memory_db()
    try:
        author = await register_scholar(db, ScholarCreate(name="Author"))
        reviewer = await register_scholar(db, ScholarCreate(name="Reviewer"))

        sub = ScrollSubmission(
            title="Review flow",
            abstract="A" * 80,
            content="B" * 250,
            domain="software-engineering",
            authors=[author.scholar_id],
        )
        scroll, errors = await submit_scroll(db, sub, author.scholar_id)
        assert scroll is not None and not errors

        review_payload = ReviewSubmission(
            scroll_id=scroll.scroll_id,
            scores=ReviewScores(
                originality=7,
                methodology=7,
                significance=7,
                clarity=7,
                overall=7,
            ),
            recommendation=ReviewRecommendation.MINOR_REVISIONS,
            comments_to_authors="Solid work with a few revisions needed.",
        )

        review_none, errs = await submit_review(db, "missing-reviewer", review_payload)
        assert review_none is None
        assert "reviewer_not_found" in errs

        first_review, errs1 = await submit_review(db, reviewer.scholar_id, review_payload)
        assert first_review is not None
        assert errs1 == []

        second_same_round, errs2 = await submit_review(db, reviewer.scholar_id, review_payload)
        assert second_same_round is None
        assert "already_reviewed_this_scroll_round" in errs2

        await db.execute(
            "UPDATE scrolls SET version = 2 WHERE scroll_id = ?",
            (scroll.scroll_id,),
        )
        await db.commit()

        second_round_review, errs3 = await submit_review(db, reviewer.scholar_id, review_payload)
        assert second_round_review is not None
        assert errs3 == []
        assert second_round_review.review_round == 2
    finally:
        await db.close()


@pytest.fixture()
def _auth_env(tmp_path: Path):
    original_data_dir = settings.data_dir
    original_require = settings.security.require_api_key
    original_anon = settings.security.allow_anonymous_read
    original_keys = settings.security.api_keys_json

    settings.data_dir = tmp_path
    settings.security.require_api_key = True
    settings.security.allow_anonymous_read = False
    settings.security.api_keys_json = json.dumps(
        [
            {
                "key": "agent-key-12345678",
                "actor_id": "agent-ops-1",
                "actor_type": "agent",
                "scopes": ["*"],
            },
            {
                "key": "limited-key-12345678",
                "actor_id": "human-observer-1",
                "actor_type": "human",
                "scopes": ["scrolls:write"],
            },
        ]
    )
    reload_api_key_cache()

    try:
        yield {
            "agent": "agent-key-12345678",
            "limited": "limited-key-12345678",
        }
    finally:
        settings.data_dir = original_data_dir
        settings.security.require_api_key = original_require
        settings.security.allow_anonymous_read = original_anon
        settings.security.api_keys_json = original_keys
        reload_api_key_cache()


def test_api_auth_and_scope_enforcement(_auth_env):
    client = TestClient(app)

    r = client.get("/api/stats")
    assert r.status_code == 401

    r = client.get("/api/stats", headers={"X-API-Key": _auth_env["agent"]})
    assert r.status_code == 200

    payload = {
        "scroll_id": "AX-2026-00001",
        "reason": "scope enforcement test",
        "reporter_id": "spoofed-reporter",
    }
    r = client.post("/api/integrity/flag", json=payload, headers={"X-API-Key": _auth_env["limited"]})
    assert r.status_code == 403

    r = client.post("/api/integrity/flag", json=payload, headers={"X-API-Key": _auth_env["agent"]})
    assert r.status_code == 200

    with sqlite3.connect(settings.db_path) as conn:
        actor = conn.execute(
            "SELECT actor_id FROM audit_events WHERE action = 'scroll_flagged' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()[0]
    assert actor == "agent-ops-1"
