"""Short-code generation and validation."""

from __future__ import annotations

import re
import secrets

ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
BASE = len(ALPHABET)
DEFAULT_CODE_LENGTH = 7

ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,32}$")

RESERVED_CODES = frozenset(
    {
        "api",
        "health",
        "healthz",
        "ready",
        "readyz",
        "metrics",
        "docs",
        "redoc",
        "openapi.json",
        "static",
        "dashboard",
        "favicon.ico",
        "robots.txt",
        "admin",
        "login",
        "logout",
    }
)


class CodecError(ValueError):
    pass


def encode(number: int) -> str:
    """Encode a non-negative integer as a Base62 string."""
    if number < 0:
        raise CodecError("cannot encode a negative integer")
    if number == 0:
        return ALPHABET[0]
    digits: list[str] = []
    while number:
        number, remainder = divmod(number, BASE)
        digits.append(ALPHABET[remainder])
    return "".join(reversed(digits))


def decode(code: str) -> int:
    value = 0
    for char in code:
        index = ALPHABET.find(char)
        if index < 0:
            raise CodecError(f"invalid Base62 character: {char!r}")
        value = value * BASE + index
    return value


def generate_code(length: int = DEFAULT_CODE_LENGTH) -> str:
    if length < 3:
        raise CodecError("short-code length must be at least 3")
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def is_reserved(code: str) -> bool:
    return code.lower() in RESERVED_CODES


def validate_alias(alias: str) -> str:
    alias = alias.strip()
    if not ALIAS_PATTERN.match(alias):
        raise CodecError(
            "alias must be 3-32 characters and contain only letters, digits, '-' or '_'"
        )
    if is_reserved(alias):
        raise CodecError("alias is reserved")
    return alias
