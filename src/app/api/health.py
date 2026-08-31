"""存活与就绪探针。

`/api/v1/healthz` 只表明进程存活；`/api/v1/readyz` 聚合全部外部依赖检查，
任一不可达即返回 503 problem+json 并列出具体组件与原因。
"""

from collections.abc import Callable

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from app.api.operational_metrics import refresh_database_metrics, refresh_rabbitmq_metrics
from app.checks import check_database, check_minio, check_rabbitmq, check_titiler
from app.errors import ProblemError
from app.logging import trace_id_var
from app.settings import get_settings

router = APIRouter(tags=["运维"])


class HealthzResponse(BaseModel):
    status: str = Field(description="进程存活状态，正常时为 ok")


class ReadyzResponse(BaseModel):
    status: str = Field(description="总体就绪状态，全部依赖可用时为 ok")
    components: dict[str, str] = Field(
        description="各依赖组件状态。键为 db / minio / rabbitmq / titiler，值为 ok 或 unavailable"
    )


@router.get(
    "/metrics",
    summary="Prometheus 指标",
    description="供内网 Prometheus 抓取；Nginx 对外返回 404，不要当业务接口调用。",
)
async def metrics(request: Request) -> Response:
    """Prometheus 抓取端点；由内网 Prometheus 直接抓取，不经 Nginx 对外暴露。"""
    refresh_database_metrics(request.app.state.session_factory)
    await refresh_rabbitmq_metrics(request.app.state.settings, request.app.state.session_factory)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get(
    "/healthz",
    summary="存活探针",
    description="只表示 API 进程还活着，不检查数据库等依赖。联调请再调就绪探针。",
    response_model=HealthzResponse,
)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get(
    "/readyz",
    summary="就绪探针",
    description=(
        "检查 PostgreSQL、MinIO、RabbitMQ、TiTiler。"
        "任一不可达返回 503，并在 components 里标明失败组件。"
    ),
    response_model=ReadyzResponse,
)
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
