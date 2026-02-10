"""Alexandria configuration — all tuneable settings in one place."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_csv(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    vals = [v.strip() for v in raw.split(",") if v.strip()]
    return vals if vals else default


def _default_data_dir() -> Path:
    """Resolve the data directory: $ALEXANDRIA_DATA or ./data."""
    env = os.environ.get("ALEXANDRIA_DATA")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "data"


class PolicyConfig(BaseModel):
    """Knobs for the autonomous publishing pipeline."""

    min_reviews_normal: int = Field(default=2, ge=1, description="Minimum reviews for normal domains")
    min_reviews_high_impact: int = Field(
        default=3, ge=1, description="Minimum reviews for high-impact domains"
    )
    high_impact_domains: list[str] = Field(
        default_factory=lambda: ["ai-theory", "ai-safety", "cryptography"],
        description="Domains requiring extra review scrutiny",
    )
    accept_score_threshold: float = Field(
        default=6.0, ge=1.0, le=10.0, description="Minimum average overall score to accept"
    )
    max_revision_rounds: int = Field(
        default=3, ge=1, description="Max revision rounds before auto-reject"
    )
    min_abstract_length: int = Field(default=50, ge=10, description="Minimum abstract char length")
    min_content_length: int = Field(default=200, ge=50, description="Minimum content char length")
    plagiarism_similarity_threshold: float = Field(
        default=0.92, ge=0.0, le=1.0, description="Cosine similarity above which content is flagged"
    )
    citation_ring_threshold: int = Field(
        default=5,
        ge=2,
        description="Reciprocal citation count above which a ring is suspected",
    )
    sybil_velocity_window_hours: int = Field(
        default=1, ge=1, description="Window for submission velocity anomaly detection"
    )
    sybil_max_submissions_per_window: int = Field(
        default=10, ge=1, description="Max submissions per identity cluster per window"
    )


class SecurityConfig(BaseModel):
    """Authentication and authorization settings."""

    require_api_key: bool = Field(
        default_factory=lambda: _env_bool("ALEXANDRIA_REQUIRE_API_KEY", False),
        description="If true, API key auth is required for all mutating API endpoints",
    )
    allow_anonymous_read: bool = Field(
        default_factory=lambda: _env_bool("ALEXANDRIA_ALLOW_ANON_READ", True),
        description="If true, read-only endpoints can be accessed without API keys",
    )
    api_keys_json: str = Field(
        default_factory=lambda: os.environ.get("ALEXANDRIA_API_KEYS_JSON", ""),
        description=(
            "JSON list of key records: "
            "[{\"key\":\"...\",\"actor_id\":\"...\",\"actor_type\":\"agent|human|system\",\"scopes\":[...]}]"
        ),
    )


class RateLimitConfig(BaseModel):
    """Basic API rate limiting controls."""

    enabled: bool = Field(default_factory=lambda: _env_bool("ALEXANDRIA_RATE_LIMIT_ENABLED", True))
    requests_per_minute: int = Field(
        default_factory=lambda: _env_int("ALEXANDRIA_RATE_LIMIT_RPM", 120),
        ge=1,
    )


class ServerConfig(BaseModel):
    """Network and transport settings."""

    host: str = Field(default_factory=lambda: os.environ.get("ALEXANDRIA_HOST", "127.0.0.1"))
    rest_port: int = Field(default_factory=lambda: _env_int("ALEXANDRIA_PORT", 8000))
    mcp_transport: str = Field(
        default_factory=lambda: os.environ.get("ALEXANDRIA_MCP_TRANSPORT", "stdio"),
        description="stdio | sse | streamable-http",
    )
    trusted_hosts: list[str] = Field(
        default_factory=lambda: _env_csv(
            "ALEXANDRIA_TRUSTED_HOSTS",
            ["127.0.0.1", "localhost", "testserver"],
        ),
    )
    cors_origins: list[str] = Field(
        default_factory=lambda: _env_csv("ALEXANDRIA_CORS_ORIGINS", []),
    )
    max_request_bytes: int = Field(
        default_factory=lambda: _env_int("ALEXANDRIA_MAX_REQUEST_BYTES", 2_000_000),
        ge=1_024,
    )
    workers: int = Field(default_factory=lambda: _env_int("ALEXANDRIA_WORKERS", 1), ge=1)
    log_level: str = Field(default_factory=lambda: os.environ.get("ALEXANDRIA_LOG_LEVEL", "info"))


class Config(BaseModel):
    """Top-level Alexandria configuration."""

    environment: str = Field(default_factory=lambda: os.environ.get("ALEXANDRIA_ENV", "development"))
    data_dir: Path = Field(default_factory=_default_data_dir)
    db_filename: str = Field(default="alexandria.db")
    chroma_dir_name: str = Field(default="chroma")
    artifacts_dir_name: str = Field(default="artifacts")
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename

    @property
    def chroma_path(self) -> Path:
        return self.data_dir / self.chroma_dir_name

    @property
    def artifacts_path(self) -> Path:
        return self.data_dir / self.artifacts_dir_name

    def ensure_dirs(self) -> None:
        """Create data directories if they don't exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.chroma_path.mkdir(parents=True, exist_ok=True)
        self.artifacts_path.mkdir(parents=True, exist_ok=True)


# Singleton — importable everywhere as `from alexandria.config import settings`
settings = Config()
