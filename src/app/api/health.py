"""存活与就绪探针。

`/api/v1/healthz` 只表明进程存活；`/api/v1/readyz` 聚合全部外部依赖检查，
任一不可达即返回 503 problem+json 并列出具体组件与原因。
"""

from collections.abc import Callable

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.checks import check_database, check_minio, check_rabbitmq, check_titiler
from app.errors import ProblemError
from app.settings import get_settings

router = APIRouter(tags=["运维"])


@router.get("/metrics")
async def metrics() -> Response:
    """Prometheus 抓取端点；由内网 Prometheus 直接抓取，不经 Nginx 对外暴露。"""
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
            },
        )
    return JSONResponse(content={"status": "ok", "components": components})
