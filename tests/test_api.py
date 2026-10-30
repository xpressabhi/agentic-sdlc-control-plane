from __future__ import annotations

from fastapi.testclient import TestClient

from agentic_sdlc.shortener.ratelimit import TokenBucketLimiter


def create_link(client: TestClient, auth: dict[str, str], **overrides):
    payload = {"url": "https://example.com/landing?utm=1", **overrides}
    return client.post("/api/v1/links", json=payload, headers=auth)


def test_health_and_readiness(client: TestClient) -> None:
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/readyz").json()["status"] == "ready"


def test_auth_required(client: TestClient) -> None:
    response = client.post("/api/v1/links", json={"url": "https://example.com"})
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_api_key"


def test_create_redirect_and_metadata(client: TestClient, auth: dict[str, str]) -> None:
    response = create_link(client, auth)
    assert response.status_code == 201
    body = response.json()
    assert body["short_url"].endswith(body["code"])
    redirect = client.get(f"/{body['code']}", follow_redirects=False)
    assert redirect.status_code == 302
    assert redirect.headers["location"] == "https://example.com/landing?utm=1"
    metadata = client.get(f"/api/v1/links/{body['code']}", headers=auth)
    assert metadata.status_code == 200
    assert metadata.json()["click_count"] == 1


def test_idempotency_header(client: TestClient, auth: dict[str, str]) -> None:
    headers = {**auth, "Idempotency-Key": "request-123"}
    first = client.post("/api/v1/links", json={"url": "https://example.com"}, headers=headers)
    second = client.post("/api/v1/links", json={"url": "https://example.com"}, headers=headers)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["code"] == second.json()["code"]
    conflict = client.post(
        "/api/v1/links", json={"url": "https://example.com/other"}, headers=headers
    )
    assert conflict.status_code == 409


def test_analytics_endpoint(client: TestClient, auth: dict[str, str]) -> None:
    code = create_link(client, auth).json()["code"]
    client.get(f"/{code}", follow_redirects=False, headers={"referer": "https://news.example"})
    client.get(f"/{code}", follow_redirects=False, headers={"referer": "https://news.example"})
    response = client.get(f"/api/v1/links/{code}/analytics", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["total_clicks"] == 2
    assert body["unique_visitors"] == 1
    assert body["top_referrers"][0]["referrer"] == "https://news.example"


def test_delete(client: TestClient, auth: dict[str, str]) -> None:
    code = create_link(client, auth).json()["code"]
    assert client.delete(f"/api/v1/links/{code}", headers=auth).status_code == 204
    assert client.get(f"/api/v1/links/{code}", headers=auth).status_code == 404
    assert client.get(f"/{code}", follow_redirects=False).status_code == 404


def test_no_auth_leak_on_redirect(client: TestClient, auth: dict[str, str]) -> None:
    code = create_link(client, auth).json()["code"]
    assert client.get(f"/{code}", follow_redirects=False).status_code == 302


def test_reserved_path_not_treated_as_code(client: TestClient) -> None:
    assert client.get("/api/v1/links/missing", headers={}).status_code == 401
    assert client.get("/favicon.ico", follow_redirects=False).status_code == 404


def test_rate_limit(client: TestClient, auth: dict[str, str], app) -> None:
    app.state.management_limiter = TokenBucketLimiter(capacity=2, refill_per_second=0.01)
    assert create_link(client, auth).status_code == 201
    assert create_link(client, auth).status_code == 201
    blocked = create_link(client, auth)
    assert blocked.status_code == 429
    assert "retry-after" in blocked.headers


def test_security_headers(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "content-security-policy" in response.headers
    assert response.headers["x-request-id"]


def test_metrics_endpoint(client: TestClient) -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "asdlc_run_success_rate" in response.text
