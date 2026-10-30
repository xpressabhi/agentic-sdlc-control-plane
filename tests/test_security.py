from __future__ import annotations

import pytest

from agentic_sdlc.shortener.codec import (
    CodecError,
    decode,
    encode,
    generate_code,
    is_reserved,
    validate_alias,
)
from agentic_sdlc.shortener.security import UnsafeURLError, validate_target_url, visitor_hash


def test_base62_roundtrip() -> None:
    for number in (0, 1, 61, 62, 63, 3843, 10**12):
        assert decode(encode(number)) == number


def test_codec_errors() -> None:
    with pytest.raises(CodecError):
        encode(-1)
    with pytest.raises(CodecError):
        decode("!")
    with pytest.raises(CodecError):
        generate_code(2)


def test_reserved_and_alias_rules() -> None:
    assert is_reserved("API")
    assert not is_reserved("promo")
    assert validate_alias("promo-2026") == "promo-2026"
    with pytest.raises(CodecError):
        validate_alias("api")
    with pytest.raises(CodecError):
        validate_alias("a" * 40)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://example.com",
        "http://",
        "http://[::1]/",
        "http://[::1",
        "http://10.1.2.3/",
        "http://192.168.1.1/",
        "http://[fe80::1]/",
        "http://user:pass@example.com/",
        "http://example.com:99999/",
        "http://localhost.localdomain/",
        "http://metadata.google.internal/",
        "http://foo.local/",
        "http://example.com/\x00",
        "http://example.com/" + "a" * 2100,
    ],
)
def test_unsafe_url_matrix(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        validate_target_url(url)


def test_url_normalization() -> None:
    assert validate_target_url("HTTP://Example.COM") == "http://example.com/"
    assert (
        validate_target_url("https://example.com/path?q=1#fragment")
        == "https://example.com/path?q=1"
    )


def test_visitor_hash_is_stable_and_salted() -> None:
    assert visitor_hash("1.2.3.4", "salt-a") == visitor_hash("1.2.3.4", "salt-a")
    assert visitor_hash("1.2.3.4", "salt-a") != visitor_hash("1.2.3.4", "salt-b")
    assert len(visitor_hash(None, "salt")) == 20
