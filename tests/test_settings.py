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
