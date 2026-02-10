"""Storage layer — SQLite for structured data, ChromaDB for vector search.

Provides:
- async SQLite connection via aiosqlite
- schema creation / migration
- Alexandria ID generator (AX-YYYY-NNNNN)
- ChromaDB collection setup
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from alexandria.config import settings

# ---------------------------------------------------------------------------
# Alexandria ID Generator
# ---------------------------------------------------------------------------

_ALEX_ID_PREFIX = "AX"


def _current_year() -> int:
    return datetime.now(timezone.utc).year


async def generate_scroll_id(db: aiosqlite.Connection) -> str:
    """Generate the next sequential Alexandria ID: AX-YYYY-NNNNN."""
    year = _current_year()
    async with db.execute(
        "SELECT seq FROM id_sequence WHERE year = ?", (year,)
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        seq = 1
        await db.execute(
            "INSERT INTO id_sequence (year, seq) VALUES (?, ?)", (year, seq)
        )
    else:
        seq = row[0] + 1
        await db.execute(
            "UPDATE id_sequence SET seq = ? WHERE year = ?", (seq, year)
        )
    await db.commit()
    return f"{_ALEX_ID_PREFIX}-{year}-{seq:05d}"


# ---------------------------------------------------------------------------
# SQLite Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
-- Alexandria ID sequence tracker
CREATE TABLE IF NOT EXISTS id_sequence (
    year     INTEGER PRIMARY KEY,
    seq      INTEGER NOT NULL DEFAULT 0
);

-- Scholar profiles
CREATE TABLE IF NOT EXISTS scholars (
    scholar_id       TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    affiliation      TEXT NOT NULL DEFAULT '',
    bio              TEXT NOT NULL DEFAULT '',
    public_key       TEXT NOT NULL DEFAULT '',
    trust_tier       TEXT NOT NULL DEFAULT 'new',
    scrolls_published INTEGER NOT NULL DEFAULT 0,
    total_citations  INTEGER NOT NULL DEFAULT 0,
    h_index          INTEGER NOT NULL DEFAULT 0,
    reviews_performed INTEGER NOT NULL DEFAULT 0,
    reputation_score REAL NOT NULL DEFAULT 0.0,
    domains          TEXT NOT NULL DEFAULT '[]',
    badges           TEXT NOT NULL DEFAULT '[]',
    sanctions        TEXT NOT NULL DEFAULT '[]',
    joined_at        TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- Scrolls (manuscripts)
CREATE TABLE IF NOT EXISTS scrolls (
    scroll_id           TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    scroll_type         TEXT NOT NULL DEFAULT 'paper',
    abstract            TEXT NOT NULL DEFAULT '',
    content             TEXT NOT NULL DEFAULT '',
    keywords            TEXT NOT NULL DEFAULT '[]',
    domain              TEXT NOT NULL DEFAULT '',
    authors             TEXT NOT NULL DEFAULT '[]',
    status              TEXT NOT NULL DEFAULT 'submitted',
    version             INTEGER NOT NULL DEFAULT 1,
    revision_history    TEXT NOT NULL DEFAULT '[]',
    claims              TEXT NOT NULL DEFAULT '[]',
    artifact_bundle_id  TEXT,
    method_profile      TEXT NOT NULL DEFAULT '',
    result_summary      TEXT NOT NULL DEFAULT '',
    evidence_grade      TEXT NOT NULL DEFAULT 'ungraded',
    badges              TEXT NOT NULL DEFAULT '[]',
    references_list     TEXT NOT NULL DEFAULT '[]',
    cited_by            TEXT NOT NULL DEFAULT '[]',
    citation_count      INTEGER NOT NULL DEFAULT 0,
    decision_record_id  TEXT,
    superseded_by       TEXT,
    retraction_reason   TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    published_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_scrolls_status ON scrolls(status);
CREATE INDEX IF NOT EXISTS idx_scrolls_domain ON scrolls(domain);
CREATE INDEX IF NOT EXISTS idx_scrolls_type ON scrolls(scroll_type);

-- Reviews
CREATE TABLE IF NOT EXISTS reviews (
    review_id            TEXT PRIMARY KEY,
    scroll_id            TEXT NOT NULL,
    reviewer_id          TEXT NOT NULL,
    review_round         INTEGER NOT NULL DEFAULT 1,
    scores               TEXT NOT NULL DEFAULT '{}',
    recommendation       TEXT NOT NULL,
    comments_to_authors  TEXT NOT NULL DEFAULT '',
    suggested_edits      TEXT NOT NULL DEFAULT '[]',
    confidential_comments TEXT NOT NULL DEFAULT '',
    reviewer_confidence  REAL NOT NULL DEFAULT 0.8,
    created_at           TEXT NOT NULL,
    FOREIGN KEY (scroll_id) REFERENCES scrolls(scroll_id),
    FOREIGN KEY (reviewer_id) REFERENCES scholars(scholar_id)
);

CREATE INDEX IF NOT EXISTS idx_reviews_scroll ON reviews(scroll_id);
CREATE INDEX IF NOT EXISTS idx_reviews_reviewer ON reviews(reviewer_id);

-- Citations (directed graph: citing -> cited)
CREATE TABLE IF NOT EXISTS citations (
    citing_scroll_id TEXT NOT NULL,
    cited_scroll_id  TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (citing_scroll_id, cited_scroll_id),
    FOREIGN KEY (citing_scroll_id) REFERENCES scrolls(scroll_id),
    FOREIGN KEY (cited_scroll_id) REFERENCES scrolls(scroll_id)
);

CREATE INDEX IF NOT EXISTS idx_citations_cited ON citations(cited_scroll_id);

-- Artifact bundles
CREATE TABLE IF NOT EXISTS artifact_bundles (
    artifact_bundle_id TEXT PRIMARY KEY,
    scroll_id          TEXT NOT NULL,
    code_hash          TEXT NOT NULL DEFAULT '',
    data_hash          TEXT NOT NULL DEFAULT '',
    env_spec           TEXT NOT NULL DEFAULT '',
    run_commands       TEXT NOT NULL DEFAULT '[]',
    expected_metrics   TEXT NOT NULL DEFAULT '{}',
    random_seed        INTEGER,
    created_at         TEXT NOT NULL,
    FOREIGN KEY (scroll_id) REFERENCES scrolls(scroll_id)
);

-- Replication results
CREATE TABLE IF NOT EXISTS replications (
    replication_id     TEXT PRIMARY KEY,
    artifact_bundle_id TEXT NOT NULL,
    scroll_id          TEXT NOT NULL,
    reproducer_id      TEXT NOT NULL,
    success            INTEGER NOT NULL DEFAULT 0,
    observed_metrics   TEXT NOT NULL DEFAULT '{}',
    logs               TEXT NOT NULL DEFAULT '',
    env_used           TEXT NOT NULL DEFAULT '',
    started_at         TEXT NOT NULL,
    completed_at       TEXT,
    FOREIGN KEY (artifact_bundle_id) REFERENCES artifact_bundles(artifact_bundle_id),
    FOREIGN KEY (scroll_id) REFERENCES scrolls(scroll_id),
    FOREIGN KEY (reproducer_id) REFERENCES scholars(scholar_id)
);

-- Decision records
CREATE TABLE IF NOT EXISTS decision_records (
    decision_id     TEXT PRIMARY KEY,
    scroll_id       TEXT NOT NULL,
    decision        TEXT NOT NULL,
    rule_evaluations TEXT NOT NULL DEFAULT '[]',
    review_summary  TEXT NOT NULL DEFAULT '{}',
    explanation     TEXT NOT NULL DEFAULT '',
    decided_at      TEXT NOT NULL,
    FOREIGN KEY (scroll_id) REFERENCES scrolls(scroll_id)
);

-- Audit events (append-only)
CREATE TABLE IF NOT EXISTS audit_events (
    event_id    TEXT PRIMARY KEY,
    action      TEXT NOT NULL,
    actor_id    TEXT NOT NULL DEFAULT '',
    target_id   TEXT NOT NULL DEFAULT '',
    target_type TEXT NOT NULL DEFAULT '',
    details     TEXT NOT NULL DEFAULT '{}',
    signature   TEXT NOT NULL DEFAULT '',
    timestamp   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_events(action);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_events(actor_id);
CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_events(target_id);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_events(timestamp);

-- Sanctions
CREATE TABLE IF NOT EXISTS sanctions (
    sanction_id   TEXT PRIMARY KEY,
    scholar_id    TEXT NOT NULL,
    sanction_type TEXT NOT NULL,
    reason        TEXT NOT NULL DEFAULT '',
    scroll_id     TEXT,
    expires_at    TEXT,
    applied_at    TEXT NOT NULL,
    FOREIGN KEY (scholar_id) REFERENCES scholars(scholar_id)
);
"""


async def get_db() -> aiosqlite.Connection:
    """Open (or reuse) the SQLite database and ensure schema exists."""
    settings.ensure_dirs()
    db = await aiosqlite.connect(str(settings.db_path))
    db.row_factory = aiosqlite.Row
    # Production-friendly SQLite pragmas.
    await db.execute("PRAGMA foreign_keys = ON;")
    await db.execute("PRAGMA journal_mode = WAL;")
    await db.execute("PRAGMA synchronous = NORMAL;")
    await db.execute("PRAGMA busy_timeout = 5000;")
    await db.execute("PRAGMA temp_store = MEMORY;")
    await db.executescript(SCHEMA_SQL)
    await db.commit()
    return db


# ---------------------------------------------------------------------------
# JSON helpers for SQLite columns that store serialised data
# ---------------------------------------------------------------------------

def _json_default(obj: Any) -> Any:
    """Custom JSON serialiser that handles Pydantic models and other types."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return str(obj)


def to_json(obj: Any) -> str:
    """Serialise a Python object for storage in a TEXT column."""
    if isinstance(obj, str):
        return obj
    return json.dumps(obj, default=_json_default)


def from_json(text: str | None) -> Any:
    """Deserialise a TEXT column back to a Python object."""
    if text is None:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


# ---------------------------------------------------------------------------
# ChromaDB setup
# ---------------------------------------------------------------------------

_chroma_client = None
_chroma_collection = None

COLLECTION_NAME = "alexandria_scrolls"


def get_chroma_client():
    """Lazy-initialise the ChromaDB persistent client."""
    global _chroma_client
    if _chroma_client is None:
        import chromadb

        settings.ensure_dirs()
        _chroma_client = chromadb.PersistentClient(path=str(settings.chroma_path))
    return _chroma_client


def get_chroma_collection():
    """Get (or create) the scrolls vector collection."""
    global _chroma_collection
    if _chroma_collection is None:
        client = get_chroma_client()
        _chroma_collection = client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
    return _chroma_collection
