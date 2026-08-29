"""Celery 任务定义：栅格入库。

重试分类（架构不变量）：
- TransientError/基础设施异常 → Job RETRYING，指数退避重新入队；
- DeterministicError → Job FAILED + 版本 FAILED + 诊断落库，不自动重试；
- NeedsInputError → Job/版本 NEEDS_INPUT，等待用户补充后由 API 重新入队。
所有步骤幂等：重复投递或重试不会产生重复工件，也不会回退已完成的状态。
"""

import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from celery import Task
from celery.exceptions import MaxRetriesExceededError
from sqlalchemy.exc import SQLAlchemyError

from app.assets.enums import AssetVersionStatus
from app.assets.service import AssetService
from app.db import create_engine, make_session_factory, session_scope
from app.jobs.enums import JobStatus
from app.jobs.service import JobService
from app.processing.errors import DeterministicError, NeedsInputError, TransientError
from app.processing.raster_ingestion import IngestionContext, RasterIngestion
from app.settings import get_settings
from app.uploads.minio import MinioAdapter, MinioError
from app.worker.celery_app import celery

logger = logging.getLogger(__name__)

# 进程级引擎缓存：celery prefork 子进程各自初始化，不在父进程导入期建连
_ENGINE: Any = None


_FACTORY: Any = None


def _get_factory() -> Any:
    global _FACTORY
    if _FACTORY is None:
        _FACTORY = make_session_factory(create_engine(get_settings()))
    return _FACTORY


def _backoff_seconds(attempt: int) -> int:
    """指数退避：5s → 10s → 20s … 上限 300s。"""
    return min(5 * 2 ** max(0, attempt - 1), 300)


@celery.task(name="processing.ingest_raster", bind=True, ignore_result=True)
def ingest_raster(self: Task, job_id: str) -> None:
    settings = get_settings()
    factory = _get_factory()
    job_uuid = UUID(job_id)

    with session_scope(factory) as session:
        jobs = JobService(session)
        claim = jobs.claim_for_run(job_uuid)
        if not claim.acquired:
            # 其他 Worker 正在执行，或任务已终态：至少一次投递下必须跳过，不能凭 RUNNING 再跑一遍
            logger.info(
                "任务重复投递，忽略",
                extra={"job_id": job_id, "status": claim.job.status.value},
            )
            return
        payload = dict(claim.job.payload)
        attempt = claim.job.attempt
        max_attempts = claim.job.max_attempts

    ctx = IngestionContext(
        job_id=job_uuid,
        version_id=UUID(str(payload["asset_version_id"])),
        source_object_key=str(payload["source_object_key"]),
        source_size_bytes=int(payload["source_size_bytes"]),
        tmp_dir=Path(settings.worker_tmp_dir) / job_id,
    )
    ingestion = RasterIngestion(
        settings=settings, minio=MinioAdapter(settings), engine=_get_factory()
    )

    try:
        ingestion.run(ctx)
        logger.info("栅格入库完成", extra={"job_id": job_id})
        return
    except NeedsInputError as exc:
        with session_scope(factory) as session:
            jobs = JobService(session)
            job = jobs.get(job_uuid)
            assert job is not None
            jobs.transition(
                job,
                JobStatus.NEEDS_INPUT,
                event_type="JOB_NEEDS_INPUT",
                detail={"reason": exc.reason, "detail": exc.detail},
            )
            job.last_error = {"code": exc.reason, "detail": exc.detail, "transient": False}
            assets = AssetService(session)
            version = assets.get_version_by_id(ctx.version_id)
            assert version is not None
            assets.set_version_status(
                version,
                AssetVersionStatus.NEEDS_INPUT,
                diagnostics={"reason": exc.reason, "detail": exc.detail, "missing": ["crs"]},
            )
        logger.info("任务进入 NEEDS_INPUT", extra={"job_id": job_id, "reason": exc.reason})
        return
    except DeterministicError as exc:
        with session_scope(factory) as session:
            jobs = JobService(session)
            job = jobs.get(job_uuid)
            assert job is not None
            jobs.transition(
                job,
                JobStatus.FAILED,
                event_type="JOB_FAILED",
                detail={"code": exc.code, "detail": exc.detail, "transient": False},
            )
            job.last_error = {"code": exc.code, "detail": exc.detail, "transient": False}
            assets = AssetService(session)
            version = assets.get_version_by_id(ctx.version_id)
            assert version is not None
            assets.set_version_status(
                version,
                AssetVersionStatus.FAILED,
                diagnostics={"reason": exc.code, "detail": exc.detail},
            )
        logger.error("任务确定性失败", extra={"job_id": job_id, "code": exc.code})
        return
    except (TransientError, MinioError, SQLAlchemyError, OSError) as exc:
        # 仅明确的基础设施/资源错误重试；损坏数据与程序缺陷不能伪装成瞬时错误。
        detail = str(exc)
        with session_scope(factory) as session:
            jobs = JobService(session)
            job = jobs.get(job_uuid)
            assert job is not None
            jobs.transition(
                job,
                JobStatus.RETRYING,
                event_type="JOB_RETRYING",
                detail={"code": "TRANSIENT", "detail": detail, "transient": True},
            )
            job.last_error = {"code": "TRANSIENT", "detail": detail, "transient": True}
        logger.warning(
            "瞬时错误，按指数退避重试",
            extra={"job_id": job_id, "attempt": attempt, "detail": detail},
        )
        try:
            raise self.retry(
                countdown=_backoff_seconds(attempt), max_retries=max(0, int(max_attempts) - 1)
            )
        except MaxRetriesExceededError:
            with session_scope(factory) as session:
                jobs = JobService(session)
                job = jobs.get(job_uuid)
                assert job is not None
                jobs.transition(
                    job,
                    JobStatus.FAILED,
                    event_type="JOB_FAILED",
                    detail={"code": "TRANSIENT_EXHAUSTED", "detail": detail, "transient": True},
                )
                job.last_error = {
                    "code": "TRANSIENT_EXHAUSTED",
                    "detail": f"瞬时错误重试次数耗尽：{detail}",
                    "transient": True,
                }
                assets = AssetService(session)
                version = assets.get_version_by_id(ctx.version_id)
                assert version is not None
                assets.set_version_status(
                    version,
                    AssetVersionStatus.FAILED,
                    diagnostics={"reason": "TRANSIENT_EXHAUSTED", "detail": detail},
                )
            logger.error("瞬时错误重试耗尽，任务失败", extra={"job_id": job_id})
    except Exception as exc:
        # Worker 系统边界必须落状态和诊断；未知异常不自动重试，随后重新抛出保留堆栈。
        detail = str(exc)
        with session_scope(factory) as session:
            jobs = JobService(session)
            job = jobs.get(job_uuid)
            assert job is not None
            jobs.transition(
                job,
                JobStatus.FAILED,
                event_type="JOB_FAILED",
                detail={
                    "code": "UNEXPECTED_PROCESSING_ERROR",
                    "detail": detail,
                    "transient": False,
                },
            )
            job.last_error = {
                "code": "UNEXPECTED_PROCESSING_ERROR",
                "detail": detail,
                "transient": False,
            }
            assets = AssetService(session)
            version = assets.get_version_by_id(ctx.version_id)
            assert version is not None
            assets.set_version_status(
                version,
                AssetVersionStatus.FAILED,
                diagnostics={"reason": "UNEXPECTED_PROCESSING_ERROR", "detail": detail},
            )
        logger.exception("任务发生未分类处理错误，已按确定性失败终止", extra={"job_id": job_id})
        raise
