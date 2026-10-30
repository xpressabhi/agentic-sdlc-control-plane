"""HTTP surface for the URL shortener."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import RedirectResponse

from ..config import Settings
from ..errors import InvalidInput, RateLimited, Unauthorized
from ..models import (
    AnalyticsResponse,
    LinkCreateRequest,
    LinkListResponse,
    LinkResponse,
)
from ..shortener.codec import is_reserved
from ..shortener.ratelimit import TokenBucketLimiter
from ..shortener.repository import Link
from ..shortener.service import ShortenerService

router = APIRouter(tags=["shortener"])


def require_api_key(request: Request) -> None:
    expected = request.app.state.settings.api_key
    supplied = request.headers.get("x-api-key")
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise Unauthorized("a valid X-API-Key header is required", code="invalid_api_key")


def _limit(request: Request, limiter: TokenBucketLimiter) -> None:
    key = request.headers.get("x-api-key") or (request.client.host if request.client else "unknown")
    allowed, retry_after = limiter.allow(key)
    if not allowed:
        raise RateLimited("rate limit exceeded", retry_after=max(0.1, retry_after))


def management_rate_limit(request: Request) -> None:
    _limit(request, request.app.state.management_limiter)


def redirect_rate_limit(request: Request) -> None:
    _limit(request, request.app.state.redirect_limiter)


def _service(request: Request) -> ShortenerService:
    return request.app.state.shortener


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def to_response(link: Link, settings: Settings) -> LinkResponse:
    return LinkResponse(
        code=link.code,
        short_url=f"{settings.public_base_url.rstrip('/')}/{link.code}",
        target_url=link.target_url,
        created_at=link.created_at,
        expires_at=link.expires_at,
        deleted_at=link.deleted_at,
        click_count=link.click_count,
        metadata=link.metadata,
    )


@router.post(
    "/api/v1/links",
    response_model=LinkResponse,
    status_code=201,
    dependencies=[Depends(require_api_key), Depends(management_rate_limit)],
)
def create_link(
    payload: LinkCreateRequest,
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> LinkResponse:
    link = _service(request).create(
        url=payload.url,
        alias=payload.custom_alias,
        ttl_seconds=payload.ttl_seconds,
        metadata=payload.metadata,
        actor=request.headers.get("x-api-key", "api"),
        idempotency_key=idempotency_key,
    )
    return to_response(link, _settings(request))


@router.get(
    "/api/v1/links",
    response_model=LinkListResponse,
    dependencies=[Depends(require_api_key), Depends(management_rate_limit)],
)
def list_links(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LinkListResponse:
    items, total = _service(request).list(limit=limit, offset=offset)
    return LinkListResponse(
        items=[to_response(item, _settings(request)) for item in items], total=total
    )


@router.get(
    "/api/v1/links/{code}",
    response_model=LinkResponse,
    dependencies=[Depends(require_api_key), Depends(management_rate_limit)],
)
def get_link(code: str, request: Request) -> LinkResponse:
    return to_response(_service(request).get(code), _settings(request))


@router.delete(
    "/api/v1/links/{code}",
    status_code=204,
    dependencies=[Depends(require_api_key), Depends(management_rate_limit)],
)
def delete_link(code: str, request: Request) -> Response:
    _service(request).delete(code)
    return Response(status_code=204)


@router.get(
    "/api/v1/links/{code}/analytics",
    response_model=AnalyticsResponse,
    dependencies=[Depends(require_api_key), Depends(management_rate_limit)],
)
def link_analytics(
    code: str,
    request: Request,
    since: Annotated[str | None, Query(description="ISO-8601 lower bound")] = None,
    until: Annotated[str | None, Query(description="ISO-8601 upper bound")] = None,
) -> AnalyticsResponse:
    data = _service(request).analytics(code, since=since, until=until)
    return AnalyticsResponse(code=code, **data)


# ---------------------------------------------------------------------------
# Public redirect. Included last so it never shadows API routes.
# ---------------------------------------------------------------------------

redirect_router = APIRouter(tags=["redirect"])


@redirect_router.get("/{code}", dependencies=[Depends(redirect_rate_limit)])
def follow(code: str, request: Request) -> RedirectResponse:
    if is_reserved(code):
        raise InvalidInput("reserved path", code="reserved_path", status_code=404)
    service = _service(request)
    link = service.resolve(code)
    service.record_click(
        link,
        ip=request.client.host if request.client else None,
        referrer=request.headers.get("referer"),
        user_agent=request.headers.get("user-agent"),
        country=request.headers.get("cf-ipcountry") or request.headers.get("x-country-code"),
    )
    response = RedirectResponse(url=link.target_url, status_code=302)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex"
    return response
