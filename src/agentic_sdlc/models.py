"""Pydantic request/response models for the public API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class LinkCreateRequest(BaseModel):
    url: str = Field(..., description="Destination URL (http/https only)")
    custom_alias: str | None = Field(default=None, description="Optional custom short code")
    ttl_seconds: int | None = Field(default=None, ge=30, le=31_536_000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def strip_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("url must not be empty")
        return value

    @field_validator("custom_alias")
    @classmethod
    def strip_alias(cls, value: str | None) -> str | None:
        return value.strip() if value else None


class LinkResponse(BaseModel):
    code: str
    short_url: str
    target_url: str
    created_at: str
    expires_at: str | None
    deleted_at: str | None
    click_count: int
    metadata: dict[str, Any]


class LinkListResponse(BaseModel):
    items: list[LinkResponse]
    total: int


class DailyClicks(BaseModel):
    day: str
    clicks: int


class ReferrerCount(BaseModel):
    referrer: str
    clicks: int


class CountryCount(BaseModel):
    country: str
    clicks: int


class AnalyticsResponse(BaseModel):
    code: str
    total_clicks: int
    unique_visitors: int
    first_click_at: str | None
    last_click_at: str | None
    daily: list[DailyClicks]
    top_referrers: list[ReferrerCount]
    top_countries: list[CountryCount]


class RunLaunchRequest(BaseModel):
    scenario: str = Field(..., description="greenfield | brownfield | ambiguous")
    auto_approve: bool = Field(
        default=False,
        description="Demo/test convenience: approve requests as a recorded demo reviewer.",
    )
    requirement_override: str | None = None


class ApprovalRequest(BaseModel):
    stage_id: str | None = None
    rationale: str = Field(default="Approved", min_length=1, max_length=2000)


class RejectRequest(ApprovalRequest):
    rationale: str = Field(default="Rejected", min_length=1, max_length=2000)


class ReplanRequest(BaseModel):
    changed_stage: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=3, max_length=2000)
    targets: list[str] | None = None
    require_approval: bool | None = None


class SafeStopRequest(BaseModel):
    reason: str = Field(default="Operator requested safe stop", min_length=1, max_length=2000)


class ErrorResponse(BaseModel):
    error: str
    code: str
    details: dict[str, Any] | None = None
