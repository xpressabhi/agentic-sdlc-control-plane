from __future__ import annotations

import pytest

from agentic_sdlc.config import Settings
from agentic_sdlc.db import Database
from agentic_sdlc.errors import Conflict, InvalidInput, NotFound
from agentic_sdlc.shortener.repository import LinkRepository
from agentic_sdlc.shortener.service import ShortenerService


def service_for(settings: Settings, now: list[str] | None = None) -> ShortenerService:
    db = Database(settings.db_path)
    clock = now or ["2026-01-01T12:00:00.000Z"]
    return ShortenerService(settings, LinkRepository(db), now_fn=lambda: clock[0])


def test_create_and_resolve(settings: Settings) -> None:
    service = service_for(settings)
    link = service.create(url="https://example.com/a?b=1", actor="tester")
    assert len(link.code) == 7
    assert link.target_url == "https://example.com/a?b=1"
    assert service.resolve(link.code).code == link.code


def test_custom_alias_and_duplicate(settings: Settings) -> None:
    service = service_for(settings)
    link = service.create(url="https://example.com", alias="promo-2026")
    assert link.code == "promo-2026"
    with pytest.raises(Conflict):
        service.create(url="https://example.com/other", alias="promo-2026")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http://127.0.0.1/x",
        "http://localhost/x",
        "http://169.254.169.254/latest/meta-data",
        "http://2130706433/x",
        "http://0x7f000001/x",
        "https://user:pass@example.com/x",
        "not-a-url",
    ],
)
def test_rejects_unsafe_urls(settings: Settings, url: str) -> None:
    service = service_for(settings)
    with pytest.raises(InvalidInput):
        service.create(url=url)


def test_reserved_alias(settings: Settings) -> None:
    service = service_for(settings)
    with pytest.raises(InvalidInput):
        service.create(url="https://example.com", alias="api")


def test_idempotency(settings: Settings) -> None:
    service = service_for(settings)
    first = service.create(url="https://example.com", idempotency_key="abc")
    second = service.create(url="https://example.com", idempotency_key="abc")
    assert first.code == second.code
    with pytest.raises(Conflict):
        service.create(url="https://example.com/different", idempotency_key="abc")


def test_ttl_expiry(settings: Settings) -> None:
    clock = ["2026-01-01T12:00:00.000Z"]
    service = service_for(settings, clock)
    link = service.create(url="https://example.com", ttl_seconds=60)
    assert link.expires_at is not None
    clock[0] = "2026-01-01T12:00:59.000Z"
    assert service.resolve(link.code).code == link.code
    clock[0] = "2026-01-01T12:01:01.000Z"
    with pytest.raises(NotFound):
        service.resolve(link.code)


def test_soft_delete(settings: Settings) -> None:
    service = service_for(settings)
    link = service.create(url="https://example.com")
    service.delete(link.code)
    with pytest.raises(NotFound):
        service.resolve(link.code)
    with pytest.raises(NotFound):
        service.delete("missing")


def test_analytics(settings: Settings) -> None:
    clock = ["2026-01-01T12:00:00.000Z"]
    service = service_for(settings, clock)
    link = service.create(url="https://example.com")
    service.record_click(
        link, ip="203.0.113.4", referrer="https://ref.example", user_agent="ua", country="US"
    )
    service.record_click(
        link, ip="203.0.113.4", referrer="", user_agent="ua", country="US"
    )
    clock[0] = "2026-01-02T09:00:00.000Z"
    service.record_click(
        link, ip="198.51.100.9", referrer="https://ref.example", user_agent="ua", country="GB"
    )
    data = service.analytics(link.code)
    assert data["total_clicks"] == 3
    assert data["unique_visitors"] == 2
    assert data["daily"] == [
        {"day": "2026-01-01", "clicks": 2},
        {"day": "2026-01-02", "clicks": 1},
    ]
    assert data["top_referrers"][0] == {"referrer": "https://ref.example", "clicks": 2}
    assert data["top_countries"][0] == {"country": "US", "clicks": 2}


def test_custom_alias_validation(settings: Settings) -> None:
    service = service_for(settings)
    for alias in ("ab", "has space", "bad!", "x" * 40):
        with pytest.raises(InvalidInput):
            service.create(url="https://example.com", alias=alias)


def test_persistence_across_service_instances(settings: Settings) -> None:
    first = service_for(settings)
    link = first.create(url="https://example.com")
    second = service_for(settings)
    assert second.resolve(link.code).target_url == "https://example.com/"


def test_short_code_charset() -> None:
    from agentic_sdlc.shortener.codec import generate_code, is_reserved

    code = generate_code()
    assert len(code) == 7
    assert not is_reserved(code)
    assert code.isalnum()
