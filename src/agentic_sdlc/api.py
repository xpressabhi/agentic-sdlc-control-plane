"""FastAPI application factory wiring the shortener and the control plane."""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .db import Database
from .errors import DomainError, RateLimited
from .orchestrator.api import router as orchestrator_router
from .orchestrator.engine import OrchestrationEngine
from .orchestrator.scenarios import ScenarioRegistry
from .orchestrator.store import OrchestratorStore
from .shortener.api import redirect_router
from .shortener.api import router as shortener_router
from .shortener.ratelimit import TokenBucketLimiter
from .shortener.repository import LinkRepository
from .shortener.service import ShortenerService

WEB_DIR = Path(__file__).resolve().parent / "web"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    settings.ensure_dirs()
    db = Database(settings.db_path)
    store = OrchestratorStore(db)
    scenarios = ScenarioRegistry.load()
    engine = OrchestrationEngine(store, settings, scenarios=scenarios)
    shortener = ShortenerService(settings, LinkRepository(db))

    app = FastAPI(
        title="Agentic SDLC Control Plane + URL Shortener",
        version="1.0.0",
        description=(
            "A governed, stateful orchestration layer that takes a requirement through "
            "requirements, design, implementation, testing, documentation and release."
        ),
    )
    app.state.settings = settings
    app.state.db = db
    app.state.store = store
    app.state.engine = engine
    app.state.scenarios = scenarios
    app.state.shortener = shortener
    app.state.management_limiter = TokenBucketLimiter(settings.rate_limit_per_minute)
    app.state.redirect_limiter = TokenBucketLimiter(settings.redirect_rate_limit_per_minute)

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        headers = {}
        if isinstance(exc, RateLimited):
            headers["Retry-After"] = str(max(1, int(exc.retry_after)))
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.message, "code": exc.code},
            headers=headers,
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: https://fastapi.tiangolo.com; "
            "script-src 'self' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "connect-src 'self'; font-src 'self'; frame-ancestors 'none'"
        )
        return response

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict:
        return {"status": "ok", "version": "1.0.0"}

    @app.get("/readyz", tags=["ops"])
    def readyz() -> JSONResponse:
        try:
            with db.read() as conn:
                conn.execute("SELECT 1").fetchone()
        except Exception as exc:  # noqa: BLE001 - readiness boundary
            return JSONResponse(
                status_code=503, content={"status": "not_ready", "error": str(exc)}
            )
        return JSONResponse(status_code=200, content={"status": "ready"})

    @app.get("/metrics", tags=["ops"], response_class=PlainTextResponse)
    def prometheus_metrics() -> PlainTextResponse:
        return PlainTextResponse(
            engine.metrics.prometheus(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    app.include_router(shortener_router)
    app.include_router(orchestrator_router)

    @app.get("/", include_in_schema=False)
    @app.get("/dashboard", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    # Public redirect is included last so it cannot shadow API or dashboard routes.
    app.include_router(redirect_router)
    return app
