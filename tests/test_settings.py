"""配置校验行为：非法数据库 URL 必须在启动期快速失败且可定位。"""

import pytest
from pydantic import ValidationError

from app.settings import Settings


def test_valid_database_url_accepted() -> None:
    settings = Settings(database_url="postgresql+psycopg://app:app@db:5432/remote_scene")
    assert settings.database_url.startswith("postgresql+psycopg://")


@pytest.mark.parametrize("bad", ["not-a-url", "mysql://app:app@localhost/db", ""])
def test_invalid_database_url_fails_fast(bad: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(database_url=bad)
    assert "APP_DATABASE_URL 不合法" in str(exc_info.value)


def test_production_rejects_empty_jwt_secret() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(env="production", jwt_secret="")
    assert "APP_JWT_SECRET" in str(exc_info.value)


def test_local_allows_empty_jwt_secret() -> None:
    settings = Settings(env="local", jwt_secret="")
    assert settings.jwt_secret == ""


@pytest.mark.parametrize("ttl", [0, -1])
def test_non_positive_jwt_ttl_fails_fast(ttl: int) -> None:
    with pytest.raises(ValidationError):
        Settings(access_token_ttl_seconds=ttl)
    with pytest.raises(ValidationError):
        Settings(refresh_token_ttl_seconds=ttl)
