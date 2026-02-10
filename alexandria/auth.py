"""Authentication and authorization helpers for REST API endpoints."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable

from fastapi import Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, ValidationError

from alexandria.config import settings


class ApiKeyRecord(BaseModel):
    """Configuration record for one API key."""

    key: str = Field(min_length=8)
    actor_id: str = Field(min_length=1)
    actor_type: str = Field(default="agent")  # agent | human | system
    scopes: list[str] = Field(default_factory=list)
    key_id: str = ""


@dataclass(slots=True, frozen=True)
class AuthContext:
    """Identity and scope context attached to a request."""

    actor_id: str = ""
    actor_type: str = "anonymous"
    scopes: frozenset[str] = frozenset()
    key_id: str = ""
    authenticated: bool = False

    def has_scope(self, scope: str) -> bool:
        return "*" in self.scopes or scope in self.scopes


def _normalize_records(raw: object) -> list[ApiKeyRecord]:
    records: list[ApiKeyRecord] = []

    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                record = ApiKeyRecord(**item)
            except ValidationError:
                continue
            if not record.key_id:
                record.key_id = f"{record.actor_type}:{record.actor_id}"
            records.append(record)
    elif isinstance(raw, dict):
        # Support dict form: {"<api-key>": {"actor_id": "...", "scopes": [...]}}
        for key, meta in raw.items():
            if not isinstance(meta, dict):
                continue
            payload = {"key": key, **meta}
            try:
                record = ApiKeyRecord(**payload)
            except ValidationError:
                continue
            if not record.key_id:
                record.key_id = f"{record.actor_type}:{record.actor_id}"
            records.append(record)

    return records


@lru_cache(maxsize=1)
def _key_index() -> dict[str, ApiKeyRecord]:
    raw = settings.security.api_keys_json.strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return {rec.key: rec for rec in _normalize_records(parsed)}


def reload_api_key_cache() -> None:
    """Clear cached API keys (useful in tests or runtime key rotation hooks)."""
    _key_index.cache_clear()


def _unauthenticated_context() -> AuthContext:
    return AuthContext(authenticated=False)


def _authorized_context(record: ApiKeyRecord) -> AuthContext:
    return AuthContext(
        actor_id=record.actor_id,
        actor_type=record.actor_type,
        scopes=frozenset(record.scopes),
        key_id=record.key_id or f"{record.actor_type}:{record.actor_id}",
        authenticated=True,
    )


async def get_auth_context(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> AuthContext:
    """Resolve request auth context from API key headers."""
    key_map = _key_index()

    if not settings.security.require_api_key:
        if x_api_key:
            record = key_map.get(x_api_key)
            if record:
                return _authorized_context(record)
        return _unauthenticated_context()

    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
        )

    record = key_map.get(x_api_key)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    return _authorized_context(record)


def require_scopes(*required_scopes: str) -> Callable[[AuthContext], AuthContext]:
    """
    FastAPI dependency enforcing scopes for mutating operations.

    If auth is disabled, this returns an unauthenticated context and allows request flow.
    """
    needed = tuple(s for s in required_scopes if s)

    async def _dependency(ctx: AuthContext = Depends(get_auth_context)) -> AuthContext:
        if not ctx.authenticated:
            return ctx

        if all(ctx.has_scope(scope) for scope in needed):
            return ctx

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "insufficient_scope",
                "required_scopes": list(needed),
                "actor_id": ctx.actor_id,
                "actor_type": ctx.actor_type,
            },
        )

    return _dependency


def enforce_read_access(ctx: AuthContext) -> None:
    """Optionally require auth for read paths based on configuration."""
    if settings.security.allow_anonymous_read:
        return
    if ctx.authenticated:
        return
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")


def resolve_actor_id(explicit_id: str | None, ctx: AuthContext) -> str:
    """Use authenticated actor identity when available, otherwise fallback to explicit request field."""
    if ctx.authenticated:
        return ctx.actor_id
    return (explicit_id or "").strip()
