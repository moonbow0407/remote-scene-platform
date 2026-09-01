"""业务接口强制登录、角色分工与停用立即失效。"""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.assets.enums import AssetType
from app.assets.service import AssetService
from app.catalogs.models import Category
from app.context import ActorContext, ActorRole, bind_actor
from app.db import make_session_factory, session_scope
from app.settings import get_settings

DATABASE_URL = os.getenv("APP_INTEGRATION_DATABASE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(DATABASE_URL is None, reason="未提供 APP_INTEGRATION_DATABASE_URL"),
]

_BOOTSTRAP_USERNAME = f"it_admin_{uuid4().hex[:10]}"
_BOOTSTRAP_EMAIL = f"{_BOOTSTRAP_USERNAME}@example.local"
_BOOTSTRAP_PASSWORD = "changeme-admin"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    assert DATABASE_URL is not None
    monkeypatch.setenv("APP_DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("APP_JWT_SECRET", "integration-jwt-secret")
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("APP_BOOTSTRAP_ADMIN_USERNAME", _BOOTSTRAP_USERNAME)
    monkeypatch.setenv("APP_BOOTSTRAP_ADMIN_EMAIL", _BOOTSTRAP_EMAIL)
    monkeypatch.setenv("APP_BOOTSTRAP_ADMIN_PASSWORD", _BOOTSTRAP_PASSWORD)
    get_settings.cache_clear()
    from app.api.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def _login(client: TestClient, username: str, password: str) -> str:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_anonymous_health_and_protected_assets(client: TestClient) -> None:
    health = client.get("/api/v1/healthz")
    assert health.status_code == 200
    metrics = client.get("/api/v1/metrics")
    assert metrics.status_code == 200
    docs = client.get("/api/v1/docs")
    assert docs.status_code == 200
    verify = client.get("/api/v1/tiles/verify")
    assert verify.status_code == 401
    denied = client.get("/api/v1/assets")
    assert denied.status_code == 401
    assert denied.json()["code"] == "AUTH_REQUIRED"


def test_user_cannot_write_catalog_admin_can(client: TestClient) -> None:
    admin_token = _login(client, _BOOTSTRAP_USERNAME, _BOOTSTRAP_PASSWORD)
    suffix = uuid4().hex[:8]
    created_user = client.post(
        "/api/v1/users",
        headers=_auth(admin_token),
        json={
            "username": f"it_user_{suffix}",
            "email": f"it_user_{suffix}@example.local",
            "password": "user-pass-1",
            "role": "USER",
        },
    )
    assert created_user.status_code == 201, created_user.text
    user_id = created_user.json()["id"]
    user_token = _login(client, f"it_user_{suffix}", "user-pass-1")

    readable = client.get("/api/v1/categories", headers=_auth(user_token))
    assert readable.status_code == 200

    forbidden = client.post(
        "/api/v1/categories",
        headers=_auth(user_token),
        json={"name": f"it-cat-{suffix}"},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "AUTH_FORBIDDEN"

    me = client.get("/api/v1/auth/me", headers=_auth(admin_token))
    assert me.status_code == 200
    admin_id = me.json()["id"]
    created = client.post(
        "/api/v1/categories",
        headers=_auth(admin_token),
        json={"name": f"it-cat-{suffix}"},
    )
    assert created.status_code == 201, created.text
    assert DATABASE_URL is not None
    engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True)
    factory = make_session_factory(engine)
    with session_scope(factory) as session:
        row = session.get(Category, created.json()["id"])
        assert row is not None
        assert row.created_by == admin_id
        actor = ActorContext(
            actor_id=str(admin_id), display_name=_BOOTSTRAP_USERNAME, role=ActorRole.ADMIN
        )
        with bind_actor(actor):
            asset = AssetService(session).create_asset(
                name=f"it-asset-{suffix}",
                asset_type=AssetType.ATTACHMENT,
                original_file_name="it.bin",
                size_bytes=1,
            )
            assert asset.created_by == admin_id
            assert asset.owner_id is None
    engine.dispose()

    disabled = client.patch(
        f"/api/v1/users/{user_id}/status",
        headers=_auth(admin_token),
        json={"is_active": False},
    )
    assert disabled.status_code == 200
    blocked = client.get("/api/v1/assets", headers=_auth(user_token))
    assert blocked.status_code == 401
    assert blocked.json()["code"] == "USER_DISABLED"
