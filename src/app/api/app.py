"""FastAPI 应用工厂。

边界约定：
- 本进程不安装 GDAL 与科学计算栈；地理处理属于 Geo Worker 镜像；
- 错误统一映射为 RFC 9457 `application/problem+json`，业务失败不返回 200；
- 业务路由随各阶段（Stage 2+）按模块注册，此处不预留占位路由。
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.assets.router import router as assets_router
from app.auth.router import auth_router, users_router
from app.catalogs.router import router as catalogs_router
from app.db import create_engine, make_session_factory
from app.ecology.router import router as ecology_router
from app.errors import ProblemError
from app.jobs.router import router as jobs_router
from app.logging import configure_logging, trace_id_var
from app.monitoring.router import router as monitoring_router
from app.settings import get_settings
from app.tiles.router import router as tiles_router
from app.uploads.router import router as uploads_router
from app.vector_features.router import router as features_router

from .health import router as ops_router
from .middleware import TraceAccessMiddleware

logger = logging.getLogger(__name__)

# 业务 API 统一前缀（架构契约 §6）
API_V1_PREFIX = "/api/v1"


def _problem(
    status: int,
    code: str,
    title: str,
    detail: str | None = None,
    errors: list[dict[str, object]] | None = None,
) -> JSONResponse:
    body: dict[str, object] = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "code": code,
    }
    if detail:
        body["detail"] = detail
    if errors:
        body["errors"] = errors
    trace_id = trace_id_var.get()
    if trace_id:
        body["trace_id"] = trace_id
    return JSONResponse(status_code=status, content=body, media_type="application/problem+json")


def _sanitize_validation_errors(exc: RequestValidationError) -> list[dict[str, object]]:
    """只保留可序列化字段，避免把 pydantic 的异常上下文透传给客户端。"""
    return [
        {"loc": list(err.get("loc", ())), "msg": err.get("msg", ""), "type": err.get("type", "")}
        for err in exc.errors()
    ]


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    engine = create_engine(settings)
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    logger.info("API 进程就绪", extra={"env": settings.env})
    try:
        yield
    finally:
        engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="remote-scene-platform",
        version="0.1.0",
        lifespan=_lifespan,
        openapi_url=f"{API_V1_PREFIX}/openapi.json",
        docs_url=f"{API_V1_PREFIX}/docs",
        redoc_url=None,
    )
    app.add_middleware(TraceAccessMiddleware)
    app.include_router(ops_router, prefix=API_V1_PREFIX)
    app.include_router(auth_router, prefix=API_V1_PREFIX)
    app.include_router(users_router, prefix=API_V1_PREFIX)
    app.include_router(assets_router, prefix=API_V1_PREFIX)
    app.include_router(uploads_router, prefix=API_V1_PREFIX)
    app.include_router(jobs_router, prefix=API_V1_PREFIX)
    app.include_router(tiles_router, prefix=API_V1_PREFIX)
    app.include_router(features_router, prefix=API_V1_PREFIX)
    app.include_router(catalogs_router, prefix=API_V1_PREFIX)
    app.include_router(ecology_router, prefix=API_V1_PREFIX)
    app.include_router(monitoring_router, prefix=API_V1_PREFIX)

    def custom_openapi() -> dict[str, Any]:
        """让 OpenAPI 与实际 RFC 9457 错误媒体类型一致，避免前端按框架默认误接。"""
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        components["ProblemDetails"] = {
            "type": "object",
            "required": ["type", "title", "status", "code"],
            "properties": {
                "type": {"type": "string"},
                "title": {"type": "string"},
                "status": {"type": "integer"},
                "code": {"type": "string"},
                "detail": {"type": "string"},
                "trace_id": {"type": "string"},
                "errors": {"type": "array", "items": {"type": "object"}},
            },
        }
        problem_content = {
            "application/problem+json": {
                "schema": {"$ref": "#/components/schemas/ProblemDetails"}
            }
        }
        for path_item in schema.get("paths", {}).values():
            for method, operation in path_item.items():
                if method not in {"get", "post", "put", "patch", "delete"}:
                    continue
                responses = operation.setdefault("responses", {})
                responses["422"] = {"description": "请求或领域校验失败", "content": problem_content}
                responses.setdefault(
                    "500", {"description": "未处理的服务端错误", "content": problem_content}
                )
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]

    @app.exception_handler(ProblemError)
    async def problem_error_handler(request: Request, exc: ProblemError) -> JSONResponse:
        return _problem(exc.status, exc.code, exc.title, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _problem(
            422,
            "REQUEST_VALIDATION",
            "请求参数校验失败",
            errors=_sanitize_validation_errors(exc),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        title = exc.detail if isinstance(exc.detail, str) else HTTPStatus(exc.status_code).phrase
        return _problem(exc.status_code, f"HTTP_{exc.status_code}", title)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        # 系统边界的统一兜底：完整堆栈已记录，对外只暴露 trace_id
        logger.exception("未处理异常")
        return _problem(500, "INTERNAL_ERROR", "服务器内部错误", "请携带 trace_id 联系运维排查")

    return app
