"""URL safety validation and privacy-preserving visitor identifiers.

The shortener never fetches the target, so classic server-side SSRF is limited;
however, redirecting users into private networks or metadata endpoints is still
an abuse vector. We reject dangerous schemes and private/loopback/link-local
destinations at creation time.

Residual risk: DNS rebinding. A production deployment should resolve and pin
the destination at click time, or route through an egress proxy with a deny
list. That is documented in ``docs/RISKS.md``.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
        "instance-data",
        "169.254.169.254",
    }
)
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class UnsafeURLError(ValueError):
    pass


def _parse_host_as_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse dotted/quoted/integer/hex IPv4 forms as well as standard IPv6."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    # Integer forms: http://2130706433/ == 127.0.0.1
    if host.isdigit():
        try:
            return ipaddress.IPv4Address(int(host))
        except (ValueError, OverflowError):
            return None
    # Hex forms: http://0x7f000001/
    if host.lower().startswith("0x"):
        try:
            return ipaddress.IPv4Address(int(host, 16))
        except (ValueError, OverflowError):
            return None
    return None


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        )
    )


def validate_target_url(raw: str, *, max_length: int = 2048) -> str:
    """Validate and normalize a destination URL. Raises ``UnsafeURLError``."""
    if not isinstance(raw, str):
        raise UnsafeURLError("url must be a string")
    url = raw.strip()
    if not url:
        raise UnsafeURLError("url must not be empty")
    if len(url) > max_length:
        raise UnsafeURLError(f"url exceeds the {max_length} character limit")
    if _CONTROL_CHARS.search(url):
        raise UnsafeURLError("url contains control characters")

    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError as exc:
        raise UnsafeURLError("malformed url") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeURLError("only http and https URLs are allowed")
    if not hostname:
        raise UnsafeURLError("url must include a hostname")
    if parsed.username or parsed.password:
        raise UnsafeURLError("credentials in URLs are not allowed")

    host = hostname.lower().rstrip(".")
    if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_SUFFIXES):
        raise UnsafeURLError("destination host is not allowed")
    if host.endswith(".localhost"):
        raise UnsafeURLError("destination host is not allowed")

    ip = _parse_host_as_ip(host)
    if ip is not None and _is_blocked_ip(ip):
        raise UnsafeURLError("private, loopback and link-local destinations are not allowed")

    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeURLError("invalid port") from exc
    if port is not None and not (1 <= port <= 65535):
        raise UnsafeURLError("invalid port")

    # Rebuild without credentials/fragment surprises; preserve path and query.
    netloc = host if port is None else f"{host}:{port}"
    normalized = urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))
    return normalized


def visitor_hash(ip: str | None, salt: str) -> str:
    """Stable, salted visitor identifier. Raw IPs are never persisted."""
    digest = hashlib.sha256(f"{salt}:{ip or 'unknown'}".encode()).hexdigest()
    return digest[:20]
