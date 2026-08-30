"""存活与就绪探针。

`/api/v1/healthz` 只表明进程存活；`/api/v1/readyz` 聚合全部外部依赖检查，
任一不可达即返回 503 problem+json 并列出具体组件与原因。
"""

from collections.abc import Callable

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.operational_metrics import refresh_database_metrics, refresh_rabbitmq_metrics
from app.checks import check_database, check_minio, check_rabbitmq, check_titiler
from app.errors import ProblemError
from app.logging import trace_id_var
from app.settings import get_settings

router = APIRouter(tags=["运维"])


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    """Prometheus 抓取端点；由内网 Prometheus 直接抓取，不经 Nginx 对外暴露。"""
    refresh_database_metrics(request.app.state.session_factory)
    await refresh_rabbitmq_metrics(request.app.state.settings, request.app.state.session_factory)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    settings = get_settings()
    engine = request.app.state.engine
    checks: dict[str, Callable[[], None]] = {
        "db": lambda: check_database(engine),
        "minio": lambda: check_minio(settings),
        "rabbitmq": lambda: check_rabbitmq(settings),
        "titiler": lambda: check_titiler(settings),
    }

    components: dict[str, str] = {}
    failures: list[str] = []
    for name, check in checks.items():
        try:
            check()
        except ProblemError as exc:
            components[name] = "unavailable"
            failures.append(f"{name}: {exc.detail}")
        else:
            components[name] = "ok"

    if failures:
        trace_id = trace_id_var.get()
        return JSONResponse(
            status_code=503,
            media_type="application/problem+json",
            content={
                "type": "about:blank",
                "title": "部分依赖组件不可用",
                "status": 503,
                "code": "DEPENDENCY_UNAVAILABLE",
                "components": components,
                "detail": "；".join(failures),
                **({"trace_id": trace_id} if trace_id else {}),
            },
        )
    return JSONResponse(content={"status": "ok", "components": components})
