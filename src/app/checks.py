"""就绪检查：DB / MinIO / RabbitMQ / TiTiler。

仅用于运维观测（/api/v1/readyz），不承载业务语义；任一依赖不可达时给出可定位的中文诊断。
"""

import logging

import httpx
import sqlalchemy as sa
from botocore.config import Config as BotoConfig
from sqlalchemy.exc import SQLAlchemyError

from app.errors import ProblemError
from app.settings import Settings

logger = logging.getLogger(__name__)


def check_database(engine: sa.Engine) -> None:
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise _unavailable("PostgreSQL", f"数据库连接失败：{exc}") from exc


def check_minio(settings: Settings) -> None:
    # 延迟导入 boto3，避免未安装该依赖组的进程（Worker 镜像外的单测）承担导入成本
    import boto3

    if not settings.minio_access_key or not settings.minio_secret_key:
        raise _unavailable("MinIO", "缺少 APP_MINIO_ACCESS_KEY / APP_MINIO_SECRET_KEY 配置")
    try:
        client = boto3.client(
            "s3",
            endpoint_url=f"{'https' if settings.minio_secure else 'http'}://{settings.minio_endpoint}",
            aws_access_key_id=settings.minio_access_key,
            aws_secret_access_key=settings.minio_secret_key,
            config=BotoConfig(
                connect_timeout=settings.readiness_timeout_seconds, retries={"max_attempts": 1}
            ),
        )
        client.head_bucket(Bucket=settings.minio_bucket)
    except Exception as exc:
        raise _unavailable(
            "MinIO", f"对象存储不可达或桶 {settings.minio_bucket} 不存在：{exc}"
        ) from exc


def check_rabbitmq(settings: Settings) -> None:
    """经 Management HTTP API 探测 Broker，避免 API 镜像引入 AMQP 客户端。"""
    try:
        response = httpx.get(
            f"{settings.rabbitmq_management_url}/api/overview",
            auth=(settings.rabbitmq_management_user, settings.rabbitmq_management_password),
            timeout=settings.readiness_timeout_seconds,
        )
        response.raise_for_status()
    except Exception as exc:
        raise _unavailable("RabbitMQ", f"消息代理不可达：{exc}") from exc


def check_titiler(settings: Settings) -> None:
    try:
        response = httpx.get(
            f"{settings.titiler_url}/healthz", timeout=settings.readiness_timeout_seconds
        )
        response.raise_for_status()
    except Exception as exc:
        raise _unavailable("TiTiler", f"瓦片服务不可达：{exc}") from exc


def _unavailable(component: str, detail: str) -> ProblemError:
    logger.warning("依赖组件不可用", extra={"component": component, "detail": detail})
    return ProblemError(
        status=503,
        code="DEPENDENCY_UNAVAILABLE",
        title=f"依赖组件 {component} 不可用",
        detail=detail,
    )
