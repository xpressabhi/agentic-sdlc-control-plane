"""Application service for the URL shortener."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from ..config import Settings, utcnow_iso
from ..errors import Conflict, InvalidInput, NotFound
from .codec import CodecError, generate_code, validate_alias
from .repository import Link, LinkRepository
from .security import UnsafeURLError, validate_target_url, visitor_hash


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ShortenerService:
    def __init__(
        self,
        settings: Settings,
        repository: LinkRepository,
        *,
        now_fn: Callable[[], str] = utcnow_iso,
    ) -> None:
        self.settings = settings
        self.repo = repository
        self._now = now_fn

    # -- creation ----------------------------------------------------------

    def create(
        self,
        *,
        url: str,
        alias: str | None = None,
        ttl_seconds: int | None = None,
        metadata: dict[str, Any] | None = None,
        actor: str | None = None,
        idempotency_key: str | None = None,
    ) -> Link:
        try:
            target = validate_target_url(url, max_length=self.settings.max_url_length)
        except UnsafeURLError as exc:
            raise InvalidInput(str(exc), code="unsafe_url") from exc

        if ttl_seconds is not None and not (
            self.settings.min_ttl_seconds <= ttl_seconds <= self.settings.max_ttl_seconds
        ):
            raise InvalidInput(
                f"ttl_seconds must be between {self.settings.min_ttl_seconds} "
                f"and {self.settings.max_ttl_seconds}",
                code="invalid_ttl",
            )

        if idempotency_key:
            existing = self.repo.find_by_idempotency_key(idempotency_key)
            if existing:
                if existing.target_url != target:
                    raise Conflict(
                        "idempotency key was already used for a different destination URL",
                        code="idempotency_conflict",
                    )
                return existing

        custom = None
        if alias:
            try:
                custom = validate_alias(alias)
            except CodecError as exc:
                raise InvalidInput(str(exc), code="invalid_alias") from exc

        now = self._now()
        expires_at = None
        if ttl_seconds is not None:
            expires_at = (
                _parse_iso(now) + timedelta(seconds=ttl_seconds)
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z")

        attempts = 1 if custom else 10
        for _ in range(attempts):
            code = custom or generate_code()
            try:
                return self.repo.create(
                    code=code,
                    target_url=target,
                    created_at=now,
                    expires_at=expires_at,
                    created_by=actor,
                    idempotency_key=idempotency_key,
                    metadata=metadata or {},
                )
            except Conflict:
                if custom:
                    raise Conflict("custom alias is already in use", code="alias_taken") from None
                # Generated collision: retry with a fresh code.
                time.sleep(0.001)
        raise Conflict("could not allocate a unique short code", code="code_exhausted")

    # -- reads -------------------------------------------------------------

    def get(self, code: str, *, include_deleted: bool = False) -> Link:
        link = self.repo.find_by_code(code)
        if link is None or (link.deleted_at and not include_deleted):
            raise NotFound(f"short link {code!r} was not found", code="link_not_found")
        return link

    def resolve(self, code: str) -> Link:
        link = self.get(code)
        if link.expires_at and link.expires_at <= self._now():
            raise NotFound(f"short link {code!r} has expired", code="link_expired")
        return link

    def is_expired(self, link: Link) -> bool:
        return bool(link.expires_at and link.expires_at <= self._now())

    def list(self, *, limit: int = 50, offset: int = 0) -> tuple[list[Link], int]:
        return self.repo.list_links(limit=limit, offset=offset)

    def analytics(
        self, code: str, *, since: str | None = None, until: str | None = None
    ) -> dict[str, Any]:
        self.get(code)
        for value in (since, until):
            if value:
                try:
                    _parse_iso(value)
                except ValueError as exc:
                    raise InvalidInput(
                        "since/until must be ISO-8601 timestamps", code="invalid_time_range"
                    ) from exc
        return self.repo.analytics(code, since, until)

    # -- mutations ---------------------------------------------------------

    def record_click(
        self,
        link: Link,
        *,
        ip: str | None,
        referrer: str | None,
        user_agent: str | None,
        country: str | None,
    ) -> None:
        self.repo.record_click(
            link_id=link.id,
            code=link.code,
            clicked_at=self._now(),
            referrer=(referrer or "")[:1024] or None,
            user_agent=(user_agent or "")[:512] or None,
            visitor_hash=visitor_hash(ip, self.settings.analytics_salt),
            country=(country or "")[:8] or None,
        )

    def delete(self, code: str) -> None:
        link = self.get(code)
        self.repo.soft_delete(link.code, self._now())
