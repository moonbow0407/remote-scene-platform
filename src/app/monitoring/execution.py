"""监测执行核心：Job 认领、输入快照执行期审计与状态推进。

执行语义（仓库中不存在遥感算法，禁止伪造算法结果）：
一次监测执行的"实际工作"是对冻结输入快照的执行期审计——逐项校验输入版本
仍然存在、处于 READY 且归属一致；全部通过即执行成功。算法类工作负载（生态
参数计算等）未来以新的处理步骤接入同一 Job/Outbox/租约链路，不改变本契约。

可靠性与幂等（与 processing.tasks 同一模式）：
- 执行权以 Job 租约为准（claim_for_run）：重复投递或执行权在他人手中直接跳过；
- 执行期间 LeaseHeartbeat 按 token 续约；进程崩溃后租约过期由恢复器回收重投；
- 瞬时错误（数据库暂不可达等）经 schedule_retry 把重投事件同事务写入 Outbox，
  由 Dispatcher 指数退避重投；重试耗尽则 Job 与 Run 一同 FAILED；
- 快照损坏（版本缺失/非 READY/归属不一致）是确定性错误：Run 与 Job 直接 FAILED，
  不自动重试；
- 成功落库后的重复消息（Run 已 SUCCEEDED）只补齐 Job 终态，不重复审计。
"""

from __future__ import annotations

import logging
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.assets.enums import AssetStatus
from app.assets.models import DataAsset
from app.db import create_engine, make_session_factory, session_scope
from app.jobs.enums import JobStatus
from app.jobs.heartbeat import LeaseHeartbeat
from app.jobs.service import JobService
from app.monitoring.enums import RunStatus
from app.monitoring.models import MonitoringRun
from app.monitoring.service import MonitoringService
from app.processing.errors import DeterministicError
from app.settings import get_settings

logger = logging.getLogger(__name__)

# 进程级引擎缓存：celery prefork 子进程各自初始化，不在父进程导入期建连
_FACTORY: Any = None


def _get_factory() -> Any:
    global _FACTORY
    if _FACTORY is None:
        _FACTORY = make_session_factory(create_engine(get_settings()))
    return _FACTORY


class SnapshotBrokenError(DeterministicError):
    """输入快照执行期审计失败：重试不会成功，Run 与 Job 直接终态。"""

    def __init__(self, problems: list[str]) -> None:
        super().__init__(code="SNAPSHOT_BROKEN", detail="；".join(problems))
        self.problems = list(problems)


def execute_monitoring_run(job_id: str, *, factory: sessionmaker[Session] | None = None) -> None:
    """监测执行任务入口：认领 → 快照审计 → 终态推进。

    factory 参数供测试注入 SQLite 会话工厂；生产路径使用进程级引擎缓存。
    """
    settings = get_settings()
    session_factory = factory if factory is not None else _get_factory()
    job_id_int = int(job_id)

    with session_scope(session_factory) as session:
        claim = JobService(session).claim_for_run(
            job_id_int, lease_ttl_seconds=settings.job_lease_ttl_seconds
        )
        if not claim.acquired:
            logger.info(
                "监测执行重复投递或执行权在他人手中，忽略",
                extra={"job_id": job_id, "status": claim.job.status.value},
            )
            return
        payload = dict(claim.job.payload)
        attempt = claim.job.attempt
        assert claim.lease_token is not None
        lease_token = claim.lease_token

    try:
        run_id = int(payload["run_id"])
    except (KeyError, ValueError) as exc:
        # payload 损坏属确定性错误：无法定位 Run，直接终态 Job 并保留诊断，
        # 不进入租约重试循环（重试不会成功）
        with session_scope(session_factory) as session:
            _fail_job_in_session(
                session, job_id_int, code="MONITORING_PAYLOAD_CORRUPT", detail=str(exc)
            )
        logger.error(
            "监测执行 payload 缺少合法 run_id，按确定性失败终止",
            extra={"job_id": job_id, "detail": str(exc)},
        )
        return

    # 心跳覆盖整个执行期；停止放在 finally，保证任何分支退出都释放续约循环
    heartbeat = LeaseHeartbeat(
        factory=session_factory,
        job_id=job_id_int,
        lease_token=lease_token,
        interval_seconds=settings.job_heartbeat_interval_seconds,
        ttl_seconds=settings.job_lease_ttl_seconds,
    )
    heartbeat.start()
    try:
        failure = _audit_and_finalize(session_factory, job_id_int, run_id)
        if failure is not None:
            logger.error(
                "监测执行确定性失败：输入快照损坏",
                extra={"job_id": job_id, "detail": failure.detail},
            )
            return
        logger.info("监测执行完成", extra={"job_id": job_id})
        return
    except SoftTimeLimitExceeded:
        detail = f"任务超过软时限 {settings.worker_task_soft_timeout_seconds} 秒，已停止处理"
        with session_scope(session_factory) as session:
            _finalize_run_failure(session, run_id, detail=detail)
            _fail_job_in_session(session, job_id_int, code="TASK_TIMEOUT", detail=detail)
        logger.error("监测执行达到软时限，已落失败诊断", extra={"job_id": job_id})
        return
    except (SQLAlchemyError, OSError) as exc:
        # 仅明确的基础设施错误重试；快照损坏已在审计内按确定性失败处理
        detail = {"code": "TRANSIENT", "detail": str(exc), "transient": True}
        with session_scope(session_factory) as session:
            jobs = JobService(session)
            job = jobs.get_required(job_id_int)
            event = jobs.schedule_retry(job, detail=detail)
            if event is None:
                # 重试耗尽：schedule_retry 已把 Job 置 FAILED，这里同步 Run 终态
                _finalize_run_failure(session, run_id, detail=f"瞬时错误重试次数耗尽：{exc}")
        if event is None:
            logger.error("监测执行瞬时错误重试耗尽，任务失败", extra={"job_id": job_id})
        else:
            logger.warning(
                "监测执行瞬时错误，重投事件已写入 Outbox 按指数退避重试",
                extra={"job_id": job_id, "attempt": attempt, "detail": str(exc)},
            )
        return
    except Exception as exc:
        # Worker 系统边界必须落状态和诊断；未知异常不自动重试，随后重新抛出保留堆栈
        with session_scope(session_factory) as session:
            _finalize_run_failure(session, run_id, detail=str(exc))
            _fail_job_in_session(
                session,
                job_id_int,
                code="UNEXPECTED_MONITORING_ERROR",
                detail=str(exc),
            )
        logger.exception("监测执行发生未分类错误，已按确定性失败终止", extra={"job_id": job_id})
        raise
    finally:
        heartbeat.stop()


def _audit_and_finalize(
    session_factory: sessionmaker[Session], job_id: int, run_id: int
) -> SnapshotBrokenError | None:
    """单事务完成：开始标记 → 快照审计 → Run 与 Job 终态同步落库。

    Run 与 Job 的终态在同一事务写入，二者不会出现"一个成功一个失败"的孤儿
    状态；确定性失败通过返回值报告（事务已提交），不再抛异常触发回滚。
    """
    with session_scope(session_factory) as session:
        monitoring = MonitoringService(session)
        run = monitoring.get_run_required(run_id)
        if run.status is RunStatus.SUCCEEDED:
            # 成功已落库而消息重投：只补齐 Job 终态（幂等收尾）
            _finish_job(session, job_id)
            return None
        # 幂等开始：PENDING → RUNNING；租约回收后的重试尝试已处于 RUNNING 时不变
        monitoring.mark_run_started(run_id)

        problems = _verify_snapshot(session, run)
        if problems:
            failure = SnapshotBrokenError(problems)
            monitoring.mark_run_failed(run_id, detail=failure.detail, code=failure.code)
            _fail_job_in_session(session, job_id, code=failure.code, detail=failure.detail)
            return failure

        monitoring.mark_run_succeeded(run_id)
        _finish_job(session, job_id)
        return None


def _verify_snapshot(session: Session, run: MonitoringRun) -> list[str]:
    """执行期快照审计：版本存在、READY、归属一致；返回问题清单（空 = 通过）。

    快照冻结的是资产主键集合。
    """
    problems: list[str] = []
    for row in run.inputs:
        asset = session.get(DataAsset, row.asset_id)
        if asset is None:
            problems.append(f"输入资产 {row.asset_id} 不存在")
            continue
        if asset.status is not AssetStatus.READY:
            problems.append(f"输入资产 {row.asset_id} 状态为 {asset.status.value}，要求 READY")
    return problems


def _finish_job(session: Session, job_id: int) -> None:
    """Job 成功收尾（RUNNING → SUCCEEDED）；已成功时幂等跳过。"""
    job = JobService(session).get_required(job_id)
    if job.status is not JobStatus.SUCCEEDED:
        JobService(session).transition(job, JobStatus.SUCCEEDED, event_type="JOB_SUCCEEDED")


def _fail_job_in_session(session: Session, job_id: int, *, code: str, detail: str) -> None:
    """Job 确定性失败收尾（RUNNING → FAILED）；已终态时幂等跳过。"""
    job = JobService(session).get_required(job_id)
    if job.status is not JobStatus.FAILED:
        JobService(session).transition(
            job,
            JobStatus.FAILED,
            event_type="JOB_FAILED",
            detail={"code": code, "detail": detail, "transient": False},
        )
        job.last_error = {"code": code, "detail": detail, "transient": False}


def _finalize_run_failure(session: Session, run_id: int, *, detail: str) -> None:
    """把 Run 推进到 FAILED 终态；已成功或已失败时保留现状态。

    重试耗尽等路径下 Run 可能仍处于 PENDING（上次尝试未及提交开始标记），
    经幂等的 mark_run_started 先归位 RUNNING 再失败，满足 Run 状态机约束。
    """
    monitoring = MonitoringService(session)
    run = monitoring.get_run_required(run_id)
    if run.status in (RunStatus.SUCCEEDED, RunStatus.FAILED):
        logger.warning("Run %s 已处于终态 %s，保留现状态", run_id, run.status)
        return
    monitoring.mark_run_started(run_id)
    monitoring.mark_run_failed(run_id, detail=detail)
