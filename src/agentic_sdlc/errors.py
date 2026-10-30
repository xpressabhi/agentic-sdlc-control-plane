"""Domain errors mapped to HTTP responses by the API layer."""

from __future__ import annotations


class DomainError(Exception):
    status_code = 400
    code = "domain_error"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code


class InvalidInput(DomainError):
    status_code = 422
    code = "invalid_input"


class NotFound(DomainError):
    status_code = 404
    code = "not_found"


class Conflict(DomainError):
    status_code = 409
    code = "conflict"


class Unauthorized(DomainError):
    status_code = 401
    code = "unauthorized"


class RateLimited(DomainError):
    status_code = 429
    code = "rate_limited"

    def __init__(self, message: str, retry_after: float = 1.0):
        super().__init__(message)
        self.retry_after = retry_after


class PolicyDenied(DomainError):
    status_code = 403
    code = "policy_denied"


class StageExecutionError(Exception):
    """Raised by agents when a stage cannot complete."""

    def __init__(self, message: str, *, retryable: bool = True, code: str = "stage_failed"):
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.code = code
