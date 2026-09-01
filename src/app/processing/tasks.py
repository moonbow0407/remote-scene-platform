"""Celery 任务定义：栅格 / 矢量 / 附件入库。

重试分类（架构不变量）：
- TransientError/基础设施异常 → Job RETRYING，重投事件与状态转换同事务写入
  Transactional Outbox，由 Dispatcher 按指数退避重新投递（禁止 Celery self.retry：
  PostgreSQL 与 RabbitMQ 双写无法原子，发布失败会留下 RETRYING 但永远没有消息的死窗口）；
- DeterministicError → Job FAILED + 资产 FAILED + 诊断落库，不自动重试；
- NeedsInputError → Job/资产 NEEDS_INPUT，等待用户补充后由 API 重新入队。

执行权与崩溃恢复：认领时取得租约并在运行期间由后台心跳续约；Worker 崩溃后租约
过期，由独立恢复器（app.recovery）回收重投，不依赖 Broker 重投消息恰好到达。
所有步骤幂等：重复投递或重试不会产生重复工件，也不会回退已完成的状态。
Job 已删除（资产清理 CASCADE/显式收掉）时必须正常返回以 ACK 投递：acks_late
下抛异常会把消息重新入队，堵塞共享 geo 队列。
"""

import logging
from pathlib import Path
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded
from celery import Task
from sqlalchemy.exc import SQLAlchemyError

from app.assets.enums import AssetStatus
from app.assets.service import AssetService
from app.db import create_engine, make_session_factory, session_scope
from app.jobs.enums import JobStatus
from app.jobs.heartbeat import LeaseHeartbeat
from app.jobs.models import Job
from app.jobs.service import JobService
from app.processing.attachment_ingestion import AttachmentIngestion
from app.processing.common import IngestionContext
from app.processing.errors import (
    DeterministicError,
    NeedsInputError,
    ProcessingCancelledError,
    TransientError,
)
from app.processing.raster_ingestion import RasterIngestion
from app.processing.vector_ingestion import VectorIngestion
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


# NEEDS_INPUT 诊断中的缺失字段：说明等待用户补充的是什么（随 reason 区分）
_MISSING_FIELDS_BY_REASON: dict[str, list[str]] = {
    "MISSING_CRS": ["crs"],
    "INVALID_CRS": ["crs"],
    "MISSING_GEOLOCATION": ["geolocation"],
}


def _load_job(session: Any, job_id: int, job_id_label: str) -> Job | None:
    """终态落库时取 Job；已删除则返回 None，调用方必须 ACK 而非抛错。"""
    job = JobService(session).get(job_id)
    if job is None:
        logger.info("任务已删除，确认投递", extra={"job_id": job_id_label})
    return job


def _execute_ingestion(self: Any, job_id: str, runner: Any, label: str) -> None:
    settings = get_settings()
    factory = _get_factory()
    job_id_int = int(job_id)

    with session_scope(factory) as session:
        jobs = JobService(session)
        claim = jobs.claim_for_run(job_id_int, lease_ttl_seconds=settings.job_lease_ttl_seconds)
        if claim is None:
            logger.info("任务已删除，确认投递", extra={"job_id": job_id})
            return
        if not claim.acquired:
            logger.info(
                "任务重复投递或执行权在他人手中，忽略",
                extra={"job_id": job_id, "status": claim.job.status.value},
            )
            return
        payload = dict(claim.job.payload)
        attempt = claim.job.attempt
        assert claim.lease_token is not None
        lease_token = claim.lease_token

    ctx = IngestionContext(
        job_id=job_id_int,
        asset_id=int(payload["asset_id"]),
        source_object_key=str(payload["source_object_key"]),
        source_size_bytes=int(payload["source_size_bytes"]),
        tmp_dir=Path(settings.worker_tmp_dir) / job_id,
    )
    ingestion = runner(settings=settings, minio=MinioAdapter(settings), engine=_get_factory())

    # 心跳覆盖整个执行期（含各异常处理分支）；进程死亡时线程随之消失，租约到期后
    # 由恢复器回收。停止放在 finally，保证任何分支退出都释放续约循环。
    heartbeat = LeaseHeartbeat(
        factory=factory,
        job_id=job_id_int,
        lease_token=lease_token,
        interval_seconds=settings.job_heartbeat_interval_seconds,
        ttl_seconds=settings.job_lease_ttl_seconds,
    )
    heartbeat.start()
    try:
        ingestion.run(ctx)
        logger.info(f"{label}入库完成", extra={"job_id": job_id})
        return
    except NeedsInputError as exc:
        with session_scope(factory) as session:
            jobs = JobService(session)
            job = _load_job(session, job_id_int, job_id)
            if job is None:
                return
            jobs.transition(
                job,
                JobStatus.NEEDS_INPUT,
                event_type="JOB_NEEDS_INPUT",
                detail={"reason": exc.reason, "detail": exc.detail},
            )
            job.last_error = {"code": exc.reason, "detail": exc.detail, "transient": False}
            assets = AssetService(session)
            asset = assets.get_asset_by_id(ctx.asset_id)
            assert asset is not None
            assets.set_status(
                asset,
                AssetStatus.NEEDS_INPUT,
                diagnostics={
                    "reason": exc.reason,
                    "detail": exc.detail,
                    "missing": _MISSING_FIELDS_BY_REASON.get(exc.reason, []),
                },
            )
        logger.info("任务进入 NEEDS_INPUT", extra={"job_id": job_id, "reason": exc.reason})
        return
    except ProcessingCancelledError:
        # 检查点已把 Job 推进到 CANCELLED；这里只收敛版本状态。
        with session_scope(factory) as session:
            AssetService(session).mark_cancelled(ctx.asset_id)
        logger.info("任务已在处理步骤检查点取消", extra={"job_id": job_id})
        return
    except SoftTimeLimitExceeded:
        detail = f"任务超过软时限 {settings.worker_task_soft_timeout_seconds} 秒，已停止处理"
        with session_scope(factory) as session:
            jobs = JobService(session)
            job = _load_job(session, job_id_int, job_id)
            if job is None:
                return
            jobs.transition(
                job,
                JobStatus.FAILED,
                event_type="JOB_TIMEOUT",
                detail={"code": "TASK_TIMEOUT", "detail": detail, "transient": False},
            )
            job.last_error = {"code": "TASK_TIMEOUT", "detail": detail, "transient": False}
            AssetService(session).mark_cancelled(ctx.asset_id, reason="TASK_TIMEOUT")
        logger.error("任务达到软时限，已落失败诊断", extra={"job_id": job_id})
        return
    except DeterministicError as exc:
        with session_scope(factory) as session:
            jobs = JobService(session)
            job = _load_job(session, job_id_int, job_id)
            if job is None:
                return
            jobs.transition(
                job,
                JobStatus.FAILED,
                event_type="JOB_FAILED",
                detail={"code": exc.code, "detail": exc.detail, "transient": False},
            )
            job.last_error = {"code": exc.code, "detail": exc.detail, "transient": False}
            assets = AssetService(session)
            asset = assets.get_asset_by_id(ctx.asset_id)
            assert asset is not None
            assets.set_status(
                asset,
                AssetStatus.FAILED,
                diagnostics={"reason": exc.code, "detail": exc.detail},
            )
        logger.error("任务确定性失败", extra={"job_id": job_id, "code": exc.code})
        return
    except (TransientError, MinioError, SQLAlchemyError, OSError) as exc:
        # 仅明确的基础设施/资源错误重试；损坏数据与程序缺陷不能伪装成瞬时错误。
        detail = {"code": "TRANSIENT", "detail": str(exc), "transient": True}
        with session_scope(factory) as session:
            jobs = JobService(session)
            job = _load_job(session, job_id_int, job_id)
            if job is None:
                return
            event = jobs.schedule_retry(job, detail=detail)
            if event is None:
                # 重试次数耗尽：schedule_retry 已把 Job 置 FAILED，这里同步版本终态
                assets = AssetService(session)
                asset = assets.get_asset_by_id(ctx.asset_id)
                assert asset is not None
                assets.set_status(
                    asset,
                    AssetStatus.FAILED,
                    diagnostics={
                        "reason": "TRANSIENT_EXHAUSTED",
                        "detail": f"瞬时错误重试次数耗尽：{exc}",
                    },
                )
        if event is None:
            logger.error("瞬时错误重试耗尽，任务失败", extra={"job_id": job_id})
        else:
            logger.warning(
                "瞬时错误，重投事件已写入 Outbox 按指数退避重试",
                extra={"job_id": job_id, "attempt": attempt, "detail": str(exc)},
            )
        return
    except Exception as exc:
        # Worker 系统边界必须落状态和诊断；未知异常不自动重试，随后重新抛出保留堆栈。
        detail = str(exc)
        with session_scope(factory) as session:
            jobs = JobService(session)
            job = _load_job(session, job_id_int, job_id)
            if job is None:
                return
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
            asset = assets.get_asset_by_id(ctx.asset_id)
            assert asset is not None
            assets.set_status(
                asset,
                AssetStatus.FAILED,
                diagnostics={"reason": "UNEXPECTED_PROCESSING_ERROR", "detail": detail},
            )
        logger.exception("任务发生未分类处理错误，已按确定性失败终止", extra={"job_id": job_id})
        raise
    finally:
        heartbeat.stop()


@celery.task(name="processing.ingest_raster", bind=True, ignore_result=True)
def ingest_raster(self: Task, job_id: str) -> None:
    _execute_ingestion(self, job_id, RasterIngestion, "栅格")


@celery.task(name="processing.ingest_vector", bind=True, ignore_result=True)
def ingest_vector(self: Task, job_id: str) -> None:
    _execute_ingestion(self, job_id, VectorIngestion, "矢量")


@celery.task(name="processing.ingest_attachment", bind=True, ignore_result=True)
def ingest_attachment(self: Task, job_id: str) -> None:
    _execute_ingestion(self, job_id, AttachmentIngestion, "附件")
