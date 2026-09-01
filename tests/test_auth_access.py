"""鉴权白名单、引导管理员、令牌与 OpenAPI 安全方案（不需要真实基础设施）。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
import sqlalchemy as sa
from pydantic import ValidationError

from app.auth.access import is_public_request
from app.auth.bootstrap import bootstrap_admin
from app.auth.models import User
from app.auth.tokens import TokenType, decode_token, issue_access_token, issue_token_pair
from app.context import ActorContext, ActorRole, bind_actor, get_actor
from app.db import Base, make_session_factory, session_scope
from app.errors import ProblemError
from app.settings import Settings, get_settings


def _settings(**overrides: object) -> Settings:
    payload: dict[str, object] = {
        "database_url": "postgresql+psycopg://app:app@localhost:5432/remote_scene",
        "env": "local",
        "jwt_secret": "unit-test-jwt-secret",
        "bootstrap_admin_username": "",
        "bootstrap_admin_email": "",
        "bootstrap_admin_password": "",
    }
    payload.update(overrides)
    return Settings(**payload)  # type: ignore[arg-type]


def test_api_prefix_matches_app() -> None:
    from app.api.app import API_V1_PREFIX
    from app.auth.access import API_V1_PREFIX as AUTH_PREFIX

    assert API_V1_PREFIX == AUTH_PREFIX


def test_public_allowlist() -> None:
    assert is_public_request("POST", "/api/v1/auth/login")
    assert is_public_request("POST", "/api/v1/auth/refresh")
    assert is_public_request("GET", "/api/v1/healthz")
    assert is_public_request("GET", "/api/v1/readyz")
    assert is_public_request("GET", "/api/v1/metrics")
    assert is_public_request("GET", "/api/v1/tiles/verify")
    assert is_public_request("GET", "/api/v1/openapi.json")
    assert is_public_request("GET", "/api/v1/docs")
    assert is_public_request("GET", "/api/v1/docs/oauth2-redirect")
    assert is_public_request("get", "/api/v1/healthz/")
    assert not is_public_request("GET", "/api/v1/assets")
    assert not is_public_request("POST", "/api/v1/auth/login/extra")
    assert not is_public_request("GET", "/api/v1/auth/me")
    assert not is_public_request("POST", "/api/v1/monitoring/runs/1/succeed")


def test_bootstrap_triplet_must_be_complete() -> None:
    with pytest.raises(ValidationError):
        _settings(bootstrap_admin_username="admin")


def test_production_requires_jwt_secret() -> None:
    with pytest.raises(ValidationError):
        _settings(env="production", jwt_secret="")


def test_bind_actor_resets() -> None:
    assert get_actor().actor_id is None
    actor = ActorContext(actor_id="7", display_name="alice", role=ActorRole.USER)
    with bind_actor(actor):
        assert get_actor() is actor
    assert get_actor().actor_id is None


def test_issue_and_decode_token_pair() -> None:
    issued = issue_token_pair(
        user_id=3,
        role=ActorRole.ADMIN,
        secret="unit-secret-at-least-32-bytes-long",
        access_ttl_seconds=60,
        refresh_ttl_seconds=120,
    )
    access = decode_token(
        issued.access_token,
        secret="unit-secret-at-least-32-bytes-long",
        expected_type=TokenType.ACCESS,
    )
    assert access.user_id == 3
    assert access.role is ActorRole.ADMIN
    refresh = decode_token(
        issued.refresh_token,
        secret="unit-secret-at-least-32-bytes-long",
        expected_type=TokenType.REFRESH,
    )
    assert refresh.user_id == 3
    assert refresh.role is None
    with pytest.raises(ProblemError) as exc:
        decode_token(
            issued.refresh_token,
            secret="unit-secret-at-least-32-bytes-long",
            expected_type=TokenType.ACCESS,
        )
    assert exc.value.code == "AUTH_TOKEN_INVALID"


def test_expired_access_token() -> None:
    token = issue_access_token(
        user_id=1,
        role=ActorRole.USER,
        secret="unit-secret-at-least-32-bytes-long",
        ttl_seconds=1,
        now=datetime(2020, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(ProblemError) as exc:
        decode_token(
            token, secret="unit-secret-at-least-32-bytes-long", expected_type=TokenType.ACCESS
        )
    assert exc.value.code == "AUTH_TOKEN_EXPIRED"


def _sqlite_factory() -> sa.Engine:
    engine = sa.create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[cast(sa.Table, User.__table__)])
    return engine


def test_bootstrap_creates_admin_once() -> None:
    engine = _sqlite_factory()
    factory = make_session_factory(engine)
    settings = _settings(
        bootstrap_admin_username="admin",
        bootstrap_admin_email="admin@example.local",
        bootstrap_admin_password="changeme-admin",
    )
    bootstrap_admin(factory, settings)
    bootstrap_admin(factory, settings)
    with session_scope(factory) as session:
        users = list(session.scalars(sa.select(User)))
        assert len(users) == 1
        assert users[0].username == "admin"
        assert users[0].role is ActorRole.ADMIN
        first_hash = users[0].password_hash
    bootstrap_admin(factory, settings)
    with session_scope(factory) as session:
        user = session.scalar(sa.select(User))
        assert user is not None
        assert user.password_hash == first_hash
    engine.dispose()


def test_bootstrap_production_without_admin_fails() -> None:
    engine = _sqlite_factory()
    factory = make_session_factory(engine)
    settings = _settings(env="production", jwt_secret="production-jwt-secret")
    with pytest.raises(RuntimeError, match="至少一名启用的管理员"):
        bootstrap_admin(factory, settings)
    engine.dispose()


def test_openapi_marks_protected_routes() -> None:
    get_settings.cache_clear()
    from app.api.app import create_app

    schema = create_app().openapi()
    assert schema.get("security") == [{"BearerAuth": []}]
    missing: list[str] = []
    for path, path_item in schema.get("paths", {}).items():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            public = is_public_request(method.upper(), path)
            security = operation.get("security", schema.get("security"))
            if public and security:
                missing.append(f"{method.upper()} {path} 应匿名")
            if not public and not security:
                missing.append(f"{method.upper()} {path} 缺少 Bearer")
    assert missing == []
