"""测试公共夹具：应用实例与测试客户端。

这些是纯进程内测试（不依赖数据库/对象存储）；基础设施边界测试按验收基线
在 Compose 环境执行。
"""

from collections.abc import Iterator
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.errors import ProblemError
from app.pagination import Page, PageParams


@pytest.fixture()
def app() -> FastAPI:
    application = create_app()

    @application.get("/test/problem")
    def problem() -> None:
        raise ProblemError(
            status=409, code="TEST_CONFLICT", title="测试冲突", detail="测试冲突详情"
        )

    @application.get("/test/unhandled")
    def unhandled() -> None:
        raise ValueError("意外错误")

    @application.get("/test/page")
    def page(params: Annotated[PageParams, Depends()]) -> Page[int]:
        items = list(range(params.offset, params.offset + params.limit))
        return Page.build(items=items, total=100, params=params)

    return application


@pytest.fixture()
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
