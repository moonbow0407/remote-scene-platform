"""瓦片令牌签发与校验行为。"""

import pytest

from app.errors import ProblemError
from app.tiles.service import (
    extract_resource_from_uri,
    extract_token_from_uri,
    sign_tile_token,
    verify_tile_token,
)

SECRET = "unit-test-secret"
RESOURCE = "s3://remote-scene/artifacts/version/cog.tif"


def test_sign_and_verify_roundtrip() -> None:
    token, expires_at = sign_tile_token(
        version_id="abc-123", resource=RESOURCE, ttl_seconds=60, secret=SECRET
    )
    assert token.startswith("v1.abc-123.")
    assert verify_tile_token(token, resource=RESOURCE, secret=SECRET) == "abc-123"
    assert expires_at > 0


def test_tampered_signature_rejected() -> None:
    token, _ = sign_tile_token(
        version_id="abc-123", resource=RESOURCE, ttl_seconds=60, secret=SECRET
    )
    bad = token[:-1] + ("0" if token[-1] != "0" else "1")
    with pytest.raises(ProblemError) as exc_info:
        verify_tile_token(bad, resource=RESOURCE, secret=SECRET)
    assert exc_info.value.status == 401


def test_expired_token_rejected() -> None:
    token, _ = sign_tile_token(
        version_id="abc-123", resource=RESOURCE, ttl_seconds=-10, secret=SECRET
    )
    with pytest.raises(ProblemError) as exc_info:
        verify_tile_token(token, resource=RESOURCE, secret=SECRET)
    assert exc_info.value.status == 401


def test_missing_secret_rejects_signing() -> None:
    with pytest.raises(ProblemError) as exc_info:
        sign_tile_token(version_id="abc-123", resource=RESOURCE, ttl_seconds=60, secret="")
    assert exc_info.value.status == 503


def test_extract_token_from_original_uri() -> None:
    uri = "/tiles/cog/tiles/9/123/456.png?url=s3%3A%2F%2Fb%2Fk.tif&token=v1.abc.123.sig"
    assert extract_token_from_uri(uri) == "v1.abc.123.sig"
    assert extract_resource_from_uri(uri) == "s3://b/k.tif"


def test_token_cannot_be_reused_for_another_object() -> None:
    token, _ = sign_tile_token(
        version_id="abc-123", resource=RESOURCE, ttl_seconds=60, secret=SECRET
    )
    with pytest.raises(ProblemError):
        verify_tile_token(
            token,
            resource="s3://remote-scene/private/another-object.tif",
            secret=SECRET,
        )


def test_missing_token_in_uri_rejected() -> None:
    with pytest.raises(ProblemError):
        extract_token_from_uri("/tiles/cog/tiles/9/123/456.png?url=x")


def test_invalid_resource_in_uri_rejected() -> None:
    with pytest.raises(ProblemError):
        extract_resource_from_uri("/tiles/cog/tiles/9/123/456.png?url=http%3A%2F%2Fexample.com")
