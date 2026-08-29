"""Auth Core：密码、JWT、用户、登录、ActorContext 与授权。"""

import json
from base64 import urlsafe_b64encode
from collections.abc import Iterator
from typing import Annotated, Any, cast
from uuid import uuid4

import jwt
import pytest
import sqlalchemy as sa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.app import create_app
from app.auth.dependencies import get_optional_actor
from app.auth.models import User, user_to_actor
from app.auth.password import hash_password, verify_password
from app.auth.service import AuthService
from app.auth.tokens import TokenType, decode_token, issue_access_token, issue_refresh_token
from app.context import ActorContext, ActorRole, get_actor
from app.db import make_session_factory, session_scope
from app.errors import ProblemError
from app.ids import new_uuid7
from app.settings import Settings

JWT_SECRET = "unit-test-jwt-secret-not-for-production"
PASSWORD = "correct-horse"
SETTINGS = Settings(
    jwt_secret=JWT_SECRET,
    access_token_ttl_seconds=3600,
    refresh_token_ttl_seconds=86400,
)


def _engine() -> sa.Engine:
    engine = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    table = cast(Table, User.__table__)
    table.create(engine)
    return engine


def _app(client: TestClient) -> FastAPI:
    return cast(FastAPI, client.app)


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = _engine()
    factory = make_session_factory(engine)
    db_session = factory()
    try:
        yield db_session
    finally:
        db_session.close()
        engine.dispose()


@pytest.fixture()
def app() -> FastAPI:
    application = create_app()

    @application.get("/test/optional-actor")
    def optional_actor(
        actor: Annotated[ActorContext, Depends(get_optional_actor)],
    ) -> dict[str, str | None]:
        return {
            "actor_id": actor.actor_id,
            "role": None if actor.role is None else actor.role.value,
        }

    return application


@pytest.fixture()
def client(app: FastAPI) -> Iterator[TestClient]:
    engine = _engine()
    with TestClient(app, raise_server_exceptions=False) as test_client:
        fastapi_app = cast(FastAPI, app)
        fastapi_app.state.engine = engine
        fastapi_app.state.session_factory = make_session_factory(engine)
        fastapi_app.state.settings = SETTINGS
        yield test_client
    engine.dispose()


def _seed(client: TestClient, **kwargs: Any) -> User:
    with session_scope(_app(client).state.session_factory) as db_session:
        return AuthService(db_session).create_user(**kwargs)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---- Password ----


def test_password_hash_is_not_plaintext() -> None:
    hashed = hash_password(PASSWORD)
    assert hashed != PASSWORD
    assert PASSWORD not in hashed
    assert hashed.startswith("$argon2id$")


def test_password_verify_success_and_failure() -> None:
    hashed = hash_password(PASSWORD)
    assert verify_password(hashed, PASSWORD) is True
    assert verify_password(hashed, "wrong-password") is False
    assert verify_password("not-a-valid-hash", PASSWORD) is False


# ---- User / ActorContext ----


def test_create_user_and_actor_mapping(session: Session) -> None:
    user = AuthService(session).create_user(
        username="alice",
        email="alice@example.com",
        password=PASSWORD,
        role=ActorRole.USER,
    )
    assert user.username == "alice"
    assert user.email == "alice@example.com"
    assert user.password_hash != PASSWORD
    actor = user_to_actor(user)
    assert actor.actor_id == str(user.id)
    assert actor.display_name == "alice"
    assert actor.role == ActorRole.USER


def test_duplicate_username_conflicts(session: Session) -> None:
    service = AuthService(session)
    service.create_user(username="alice", email="a@example.com", password=PASSWORD)
    with pytest.raises(ProblemError) as exc_info:
        service.create_user(username="alice", email="b@example.com", password=PASSWORD)
    assert exc_info.value.status == 409
    assert exc_info.value.code == "USER_ALREADY_EXISTS"


def test_duplicate_email_conflicts(session: Session) -> None:
    service = AuthService(session)
    service.create_user(username="alice", email="same@example.com", password=PASSWORD)
    with pytest.raises(ProblemError) as exc_info:
        service.create_user(username="bob", email="same@example.com", password=PASSWORD)
    assert exc_info.value.status == 409
    assert exc_info.value.code == "USER_ALREADY_EXISTS"


def test_anonymous_actor_unchanged() -> None:
    actor = get_actor()
    assert actor.actor_id is None
    assert actor.role is None
    assert actor.display_name == "anonymous-system"


# ---- JWT ----


def test_access_token_roundtrip() -> None:
    user_id = new_uuid7()
    token = issue_access_token(
        user_id=user_id, role=ActorRole.ADMIN, secret=JWT_SECRET, ttl_seconds=60
    )
    claims = decode_token(token, secret=JWT_SECRET, expected_type=TokenType.ACCESS)
    assert claims.user_id == user_id
    assert claims.role == ActorRole.ADMIN
    assert claims.token_type == TokenType.ACCESS


def test_expired_access_token_rejected() -> None:
    token = issue_access_token(
        user_id=new_uuid7(),
        role=ActorRole.USER,
        secret=JWT_SECRET,
        ttl_seconds=-10,
    )
    with pytest.raises(ProblemError) as exc_info:
        decode_token(token, secret=JWT_SECRET, expected_type=TokenType.ACCESS)
    assert exc_info.value.status == 401
    assert exc_info.value.code == "AUTH_TOKEN_EXPIRED"


def test_tampered_access_token_rejected() -> None:
    token = issue_access_token(
        user_id=new_uuid7(), role=ActorRole.USER, secret=JWT_SECRET, ttl_seconds=60
    )
    header, payload, signature = token.split(".")
    flipped = "A" if signature[0] != "A" else "B"
    tampered = f"{header}.{payload}.{flipped}{signature[1:]}"
    with pytest.raises(ProblemError) as exc_info:
        decode_token(tampered, secret=JWT_SECRET, expected_type=TokenType.ACCESS)
    assert exc_info.value.status == 401
    assert exc_info.value.code == "AUTH_TOKEN_INVALID"


def test_refresh_token_cannot_decode_as_access() -> None:
    token = issue_refresh_token(user_id=new_uuid7(), secret=JWT_SECRET, ttl_seconds=60)
    with pytest.raises(ProblemError) as exc_info:
        decode_token(token, secret=JWT_SECRET, expected_type=TokenType.ACCESS)
    assert exc_info.value.code == "AUTH_TOKEN_INVALID"


def test_access_token_cannot_decode_as_refresh() -> None:
    token = issue_access_token(
        user_id=new_uuid7(), role=ActorRole.USER, secret=JWT_SECRET, ttl_seconds=60
    )
    with pytest.raises(ProblemError) as exc_info:
        decode_token(token, secret=JWT_SECRET, expected_type=TokenType.REFRESH)
    assert exc_info.value.code == "AUTH_TOKEN_INVALID"


def test_illegal_token_type_rejected() -> None:
    user_id = new_uuid7()
    token = jwt.encode(
        {
            "sub": str(user_id),
            "token_type": "other",
            "iat": 1_700_000_000,
            "exp": 2_000_000_000,
        },
        JWT_SECRET,
        algorithm="HS256",
    )
    with pytest.raises(ProblemError) as exc_info:
        decode_token(token, secret=JWT_SECRET, expected_type=TokenType.ACCESS)
    assert exc_info.value.code == "AUTH_TOKEN_INVALID"


def test_alg_none_token_rejected() -> None:
    def _b64(payload: dict[str, object]) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = _b64({"alg": "none", "typ": "JWT"})
    payload = _b64(
        {
            "sub": str(new_uuid7()),
            "token_type": "access",
            "role": "ADMIN",
            "iat": 1,
            "exp": 2_000_000_000,
        }
    )
    with pytest.raises(ProblemError) as exc_info:
        decode_token(f"{header}.{payload}.", secret=JWT_SECRET, expected_type=TokenType.ACCESS)
    assert exc_info.value.status == 401


def test_empty_jwt_secret_rejected() -> None:
    with pytest.raises(ProblemError) as exc_info:
        issue_access_token(user_id=new_uuid7(), role=ActorRole.USER, secret="", ttl_seconds=60)
    assert exc_info.value.status == 503


# ---- Login / Refresh / Me API ----


def test_login_success_and_me(client: TestClient) -> None:
    user = _seed(
        client, username="alice", email="alice@example.com", password=PASSWORD, role=ActorRole.USER
    )
    response = client.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"access_token", "refresh_token", "token_type", "expires_in"}
    assert "password" not in body
    assert "password_hash" not in body
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 3600

    me = client.get("/api/v1/auth/me", headers=_auth(body["access_token"]))
    assert me.status_code == 200
    public = me.json()
    assert public["id"] == str(user.id)
    assert public["username"] == "alice"
    assert public["role"] == "USER"
    assert "password_hash" not in public


def test_login_with_email(client: TestClient) -> None:
    _seed(client, username="alice", email="alice@example.com", password=PASSWORD)
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "alice@example.com", "password": PASSWORD},
    )
    assert response.status_code == 200


def test_login_wrong_password_is_401(client: TestClient) -> None:
    _seed(client, username="alice", email="alice@example.com", password=PASSWORD)
    response = client.post(
        "/api/v1/auth/login", json={"username": "alice", "password": "wrong-password"}
    )
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "AUTH_INVALID_CREDENTIALS"


def test_login_unknown_user_same_as_wrong_password(client: TestClient) -> None:
    response = client.post("/api/v1/auth/login", json={"username": "nobody", "password": PASSWORD})
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_INVALID_CREDENTIALS"
    assert response.json()["detail"] == "用户名或密码错误"


def test_disabled_user_cannot_login(client: TestClient) -> None:
    _seed(
        client,
        username="alice",
        email="alice@example.com",
        password=PASSWORD,
        is_active=False,
    )
    response = client.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD})
    assert response.status_code == 401
    assert response.json()["code"] == "USER_DISABLED"


def test_me_requires_access_token(client: TestClient) -> None:
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_REQUIRED"


def test_refresh_token_cannot_access_me(client: TestClient) -> None:
    _seed(client, username="alice", email="alice@example.com", password=PASSWORD)
    login = client.post(
        "/api/v1/auth/login", json={"username": "alice", "password": PASSWORD}
    ).json()
    response = client.get("/api/v1/auth/me", headers=_auth(login["refresh_token"]))
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_TOKEN_INVALID"


def test_refresh_success_reissues_tokens(client: TestClient) -> None:
    _seed(client, username="alice", email="alice@example.com", password=PASSWORD)
    login = client.post(
        "/api/v1/auth/login", json={"username": "alice", "password": PASSWORD}
    ).json()
    response = client.post("/api/v1/auth/refresh", json={"refresh_token": login["refresh_token"]})
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"]
    me = client.get("/api/v1/auth/me", headers=_auth(body["access_token"]))
    assert me.status_code == 200


def test_access_token_cannot_refresh(client: TestClient) -> None:
    _seed(client, username="alice", email="alice@example.com", password=PASSWORD)
    login = client.post(
        "/api/v1/auth/login", json={"username": "alice", "password": PASSWORD}
    ).json()
    response = client.post("/api/v1/auth/refresh", json={"refresh_token": login["access_token"]})
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_TOKEN_INVALID"


def test_expired_refresh_rejected(client: TestClient) -> None:
    user = _seed(client, username="alice", email="alice@example.com", password=PASSWORD)
    expired = issue_refresh_token(user_id=user.id, secret=JWT_SECRET, ttl_seconds=-10)
    response = client.post("/api/v1/auth/refresh", json={"refresh_token": expired})
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_TOKEN_EXPIRED"


def test_invalid_refresh_rejected(client: TestClient) -> None:
    response = client.post("/api/v1/auth/refresh", json={"refresh_token": "not-a-jwt"})
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_TOKEN_INVALID"


def test_expired_access_token_rejected_by_me(client: TestClient) -> None:
    user = _seed(client, username="alice", email="alice@example.com", password=PASSWORD)
    expired = issue_access_token(
        user_id=user.id, role=ActorRole.USER, secret=JWT_SECRET, ttl_seconds=-10
    )
    response = client.get("/api/v1/auth/me", headers=_auth(expired))
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_TOKEN_EXPIRED"


def test_optional_actor_anonymous_without_token(client: TestClient) -> None:
    response = client.get("/test/optional-actor")
    assert response.status_code == 200
    assert response.json()["actor_id"] is None
    assert response.json()["role"] is None


def test_optional_actor_fail_closed_on_invalid_token(client: TestClient) -> None:
    response = client.get("/test/optional-actor", headers=_auth("bad-token"))
    assert response.status_code == 401


# ---- Authorization ----


def test_user_cannot_list_or_create_users(client: TestClient) -> None:
    _seed(
        client,
        username="alice",
        email="alice@example.com",
        password=PASSWORD,
        role=ActorRole.USER,
    )
    login = client.post(
        "/api/v1/auth/login", json={"username": "alice", "password": PASSWORD}
    ).json()
    listed = client.get("/api/v1/users", headers=_auth(login["access_token"]))
    assert listed.status_code == 403
    assert listed.json()["code"] == "AUTH_FORBIDDEN"
    created = client.post(
        "/api/v1/users",
        headers=_auth(login["access_token"]),
        json={
            "username": "bob",
            "email": "bob@example.com",
            "password": PASSWORD,
            "role": "USER",
        },
    )
    assert created.status_code == 403


def test_admin_can_manage_users(client: TestClient) -> None:
    admin = _seed(
        client, username="root", email="root@example.com", password=PASSWORD, role=ActorRole.ADMIN
    )
    login = client.post(
        "/api/v1/auth/login", json={"username": "root", "password": PASSWORD}
    ).json()
    headers = _auth(login["access_token"])
    created = client.post(
        "/api/v1/users",
        headers=headers,
        json={
            "username": "bob",
            "email": "bob@example.com",
            "password": PASSWORD,
            "role": "USER",
        },
    )
    assert created.status_code == 201
    assert "password_hash" not in created.json()
    listed = client.get("/api/v1/users", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["total"] == 2
    assert {item["username"] for item in listed.json()["items"]} == {"root", "bob"}

    other_id = created.json()["id"]
    updated = client.put(
        f"/api/v1/users/{other_id}",
        headers=headers,
        json={"username": "bobby"},
    )
    assert updated.status_code == 200
    assert updated.json()["username"] == "bobby"

    disabled = client.patch(
        f"/api/v1/users/{other_id}/status",
        headers=headers,
        json={"is_active": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["is_active"] is False

    self_disable = client.patch(
        f"/api/v1/users/{admin.id}/status",
        headers=headers,
        json={"is_active": False},
    )
    assert self_disable.status_code == 403


def test_user_can_read_self_but_not_others(client: TestClient) -> None:
    alice = _seed(
        client, username="alice", email="alice@example.com", password=PASSWORD, role=ActorRole.USER
    )
    bob = _seed(
        client, username="bob", email="bob@example.com", password=PASSWORD, role=ActorRole.USER
    )
    login = client.post(
        "/api/v1/auth/login", json={"username": "alice", "password": PASSWORD}
    ).json()
    headers = _auth(login["access_token"])
    self_resp = client.get(f"/api/v1/users/{alice.id}", headers=headers)
    assert self_resp.status_code == 200
    other = client.get(f"/api/v1/users/{bob.id}", headers=headers)
    assert other.status_code == 403


def test_jwt_role_claim_cannot_override_database_role(client: TestClient) -> None:
    user = _seed(
        client, username="alice", email="alice@example.com", password=PASSWORD, role=ActorRole.USER
    )
    forged = issue_access_token(
        user_id=user.id, role=ActorRole.ADMIN, secret=JWT_SECRET, ttl_seconds=60
    )
    response = client.get("/api/v1/users", headers=_auth(forged))
    assert response.status_code == 403
    assert response.json()["code"] == "AUTH_FORBIDDEN"


def test_disabled_user_token_rejected(client: TestClient) -> None:
    user = _seed(
        client, username="alice", email="alice@example.com", password=PASSWORD, role=ActorRole.USER
    )
    login = client.post(
        "/api/v1/auth/login", json={"username": "alice", "password": PASSWORD}
    ).json()
    with session_scope(_app(client).state.session_factory) as db_session:
        AuthService(db_session).set_user_status(
            user.id,
            is_active=False,
            actor=ActorContext(actor_id=str(uuid4()), display_name="admin", role=ActorRole.ADMIN),
        )
    response = client.get("/api/v1/auth/me", headers=_auth(login["access_token"]))
    assert response.status_code == 401
    assert response.json()["code"] == "USER_DISABLED"
