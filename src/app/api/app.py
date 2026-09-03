"""FastAPI 应用工厂。

边界约定：
- 本进程不安装 GDAL 与科学计算栈；地理处理属于 Geo Worker 镜像；
- 错误统一映射为 RFC 9457 `application/problem+json`，业务失败不返回 200；
- 业务路由按模块注册，此处不预留占位路由。
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.auth.access import is_public_request
from app.auth.bootstrap import bootstrap_admin
from app.auth.dependencies import enforce_request_actor
from app.auth.router import auth_router, users_router
from app.data_sources.router import router as data_sources_router
from app.db import create_engine, make_session_factory
from app.ecology.router import router as ecology_router
from app.errors import ProblemError
from app.imagery.router import satellites_router, search_router, uavs_router
from app.jobs.router import router as jobs_router
from app.logging import configure_logging, trace_id_var
from app.mines.router import router as mines_router
from app.monitoring.router import router as monitoring_router
from app.openapi_compat import polish_openapi
from app.settings import get_settings
from app.tiles.router import router as tiles_router
from app.uploads.router import router as uploads_router

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
    bootstrap_admin(app.state.session_factory, settings)
    logger.info("API 进程就绪", extra={"env": settings.env})
    try:
        yield
    finally:
        engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="多源遥感数据共享平台",
        version="0.1.0",
        description=(
            "路径都以 `/api/v1` 开头。\n\n"
            "成功时直接返回资源对象，没有 `code` / `msg` / `data` 外层包装。\n"
            "列表一律是 `items`、`total`、`page`、`page_size`。\n"
            "GET 查询参数里的空字符串视为未传；JSON 请求体仍按类型校验。\n"
            "出错时 HTTP 状态码不是 200，类型是 `application/problem+json`，请看 `code`。\n"
            "时间一律带时区。\n"
            "空间范围只接受经纬度（EPSG:4326）的 GeoJSON 多边形（Polygon 或 MultiPolygon）。\n"
            "大文件用返回的临时地址直传，不要把文件字节 POST 到本服务。\n"
            "上传完成后轮询卫星或无人机详情的 `status`，不要轮询任务接口。\n"
            "除登录、刷新、探活、文档、指标和瓦片校验外，请求必须带 "
            "`Authorization: Bearer <access_token>`。"
        ),
        lifespan=_lifespan,
        dependencies=[Depends(enforce_request_actor)],
        servers=[
            {"url": settings.public_base_url.rstrip("/"), "description": "当前环境 API 基地址"},
        ],
        openapi_tags=[
            {"name": "运维", "description": "进程是否存活、依赖是否就绪。前端一般不用。"},
            {"name": "鉴权", "description": "登录、刷新令牌、查看当前用户。"},
            {"name": "用户", "description": "管理员创建账号、改资料、停用账号。"},
            {
                "name": "上传",
                "description": "大文件分片直传。创建会话后直传分片，再轮询卫星或无人机详情。",
            },
            {"name": "数据源", "description": "产品型号字典，例如 000114 哨兵二号。"},
            {"name": "卫星", "description": "卫星影像记录及其处理状态。"},
            {"name": "无人机", "description": "无人机影像记录及其处理状态。"},
            {"name": "检索", "description": "地图选数：同时检索卫星和无人机。"},
            {"name": "任务", "description": "后台处理进度。管理页面请看影像状态，不必调这里。"},
            {"name": "瓦片", "description": "栅格在地图上显示用的短期地址。过期后重新申请。"},
            {"name": "生态", "description": "生态参数，以及它和产品型号的对应关系。"},
            {"name": "监测", "description": "按范围和周期自动挑选已处理完成的数据并执行。"},
            {"name": "矿山", "description": "矿山基础信息及其 EPSG:4326 空间覆盖范围。"},
        ],
        openapi_url=f"{API_V1_PREFIX}/openapi.json",
        docs_url=f"{API_V1_PREFIX}/docs",
        redoc_url=None,
    )
    app.add_middleware(TraceAccessMiddleware)
    app.include_router(ops_router, prefix=API_V1_PREFIX)
    app.include_router(auth_router, prefix=API_V1_PREFIX)
    app.include_router(users_router, prefix=API_V1_PREFIX)
    app.include_router(uploads_router, prefix=API_V1_PREFIX)
    app.include_router(data_sources_router, prefix=API_V1_PREFIX)
    app.include_router(satellites_router, prefix=API_V1_PREFIX)
    app.include_router(uavs_router, prefix=API_V1_PREFIX)
    app.include_router(search_router, prefix=API_V1_PREFIX)
    app.include_router(jobs_router, prefix=API_V1_PREFIX)
    app.include_router(tiles_router, prefix=API_V1_PREFIX)
    app.include_router(ecology_router, prefix=API_V1_PREFIX)
    app.include_router(monitoring_router, prefix=API_V1_PREFIX)
    app.include_router(mines_router, prefix=API_V1_PREFIX)

    def custom_openapi() -> dict[str, Any]:
        """让 OpenAPI 与实际 RFC 9457 错误媒体类型一致，避免前端按框架默认误接。"""
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
            tags=app.openapi_tags,
            servers=[
                {
                    "url": settings.public_base_url.rstrip("/"),
                    "description": "当前环境 API 基地址",
                }
            ],
        )
        components_root = schema.setdefault("components", {})
        components_root.setdefault("securitySchemes", {})["BearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "登录后获得的 access_token",
        }
        schema["security"] = [{"BearerAuth": []}]
        components = components_root.setdefault("schemas", {})
        components["ProblemDetails"] = {
            "type": "object",
            "title": "错误信息",
            "description": "出错时的响应体。HTTP 状态码不是 200。请根据 code 判断错误类型。",
            "required": ["type", "title", "status", "code"],
            "properties": {
                "type": {"type": "string", "description": "固定为 about:blank，可忽略"},
                "title": {"type": "string", "description": "简短错误标题"},
                "status": {"type": "integer", "description": "HTTP 状态码"},
                "code": {
                    "type": "string",
                    "description": "错误码，例如 REQUEST_VALIDATION。前端按这个分支处理",
                },
                "detail": {"type": "string", "description": "更具体的说明"},
                "trace_id": {
                    "type": "string",
                    "description": "这次请求的追踪编号，报障时带给后端",
                },
                "errors": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "哪个字段不合法，多见于 422",
                },
            },
        }
        for model in components.values():
            if model.get("title") == "分页结果" and not model.get("description"):
                model["description"] = "一页数据。所有列表都是这个结构。"
        problem_content = {
            "application/problem+json": {"schema": {"$ref": "#/components/schemas/ProblemDetails"}}
        }
        for path, path_item in schema.get("paths", {}).items():
            for method, operation in path_item.items():
                if method not in {"get", "post", "put", "patch", "delete"}:
                    continue
                if is_public_request(method.upper(), path):
                    operation["security"] = []
                responses = operation.setdefault("responses", {})
                responses["422"] = {"description": "请求参数不合法", "content": problem_content}
                responses.setdefault(
                    "500", {"description": "服务器内部错误", "content": problem_content}
                )
                if not is_public_request(method.upper(), path):
                    responses.setdefault(
                        "401", {"description": "未认证", "content": problem_content}
                    )
        schema = polish_openapi(schema)
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
