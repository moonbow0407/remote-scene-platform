"""独立 Scheduler 进程入口。

职责（架构边界）：评估到期监测计划，为每个到期周期生成 occurrence 并派发
监测执行；occurrence 的 (plan_id, scheduled_for) 数据库唯一保证"同一计划时刻
至多一次执行"。本进程只负责"什么时候扫描、谁来扫描"：

- PostgreSQL 会话级 advisory lock 保证同一时刻只有一个 Scheduler 实例执行
  扫描/派发（多实例部署下其余实例直接跳过本轮，不排队等待）；
- 停机恢复只补跑最近一次错过周期，其余记录 MISSED（MonitoringService）；
- 每个 tick 一个数据库事务：occurrence/Run/输入快照/派发要么整体提交，
  要么整体回滚，重启后重新计算，不产生半状态。
"""

import hashlib
import logging
import signal
import time
from types import FrameType

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.context import now_utc
from app.db import create_engine, make_session_factory, session_scope
from app.logging import configure_logging
from app.monitoring.service import DeferredRunDispatcher, MonitoringService
from app.settings import get_settings

logger = logging.getLogger("app.scheduler")

_PLAN_TABLE = "monitoring_plan"
_POLL_SECONDS = 10.0
# 会话级 advisory lock 键：由稳定字符串派生的 64 位整数，跨进程/实例一致
_SCHEDULER_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"monitoring:scheduler:v1").digest()[:8], "big", signed=True
)


class _StopFlag:
    def __init__(self) -> None:
        self.stopped = False

    def request_stop(self, signum: int, frame: FrameType | None) -> None:
        self.stopped = True


def _table_exists(engine: sa.Engine, table: str) -> bool:
    return sa.inspect(engine).has_table(table)


def _try_advisory_lock(connection: sa.Connection) -> bool:
    """尝试取得 Scheduler 互斥锁；pg_try_advisory_lock 立即返回不阻塞。"""
    return bool(
        connection.execute(
            sa.text("SELECT pg_try_advisory_lock(:key)"), {"key": _SCHEDULER_LOCK_KEY}
        ).scalar()
    )


def _release_advisory_lock(connection: sa.Connection) -> None:
    connection.execute(sa.text("SELECT pg_advisory_unlock(:key)"), {"key": _SCHEDULER_LOCK_KEY})


def _run_tick(session: Session) -> None:
    summary = MonitoringService(session, DeferredRunDispatcher()).process_due_plans(now=now_utc())
    if summary.plans_considered or summary.missed_recorded:
        logger.info(
            "调度扫描完成",
            extra={
                "plans_considered": summary.plans_considered,
                "dispatched": summary.dispatched,
                "missed_recorded": summary.missed_recorded,
                "skipped": [str(plan_id) for plan_id in summary.skipped_plan_ids],
            },
        )


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    stop = _StopFlag()
    signal.signal(signal.SIGTERM, stop.request_stop)
    signal.signal(signal.SIGINT, stop.request_stop)

    engine = create_engine(settings)
    session_factory = make_session_factory(engine)
    logger.info("Scheduler 已启动", extra={"poll_seconds": _POLL_SECONDS})
    announced_ready = False
    try:
        while not stop.stopped:
            try:
                if not _table_exists(engine, _PLAN_TABLE):
                    logger.info(
                        "等待 monitoring_plan 表（Stage 5 迁移创建）", extra={"table": _PLAN_TABLE}
                    )
                    time.sleep(_POLL_SECONDS)
                    continue
                if not announced_ready:
                    logger.info("monitoring_plan 表已就绪，周期评估循环开始")
                    announced_ready = True
                # 锁连接与工作事务分离：锁随连接持有，扫描在独立事务中提交
                with engine.connect() as lock_connection:
                    if not _try_advisory_lock(lock_connection):
                        logger.debug("其他 Scheduler 实例持有锁，跳过本轮扫描")
                        time.sleep(_POLL_SECONDS)
                        continue
                    try:
                        with session_scope(session_factory) as session:
                            _run_tick(session)
                    finally:
                        _release_advisory_lock(lock_connection)
            except SQLAlchemyError as exc:
                logger.warning("数据库暂不可达，稍后重试", extra={"detail": str(exc)})
            time.sleep(_POLL_SECONDS)
    finally:
        engine.dispose()
        logger.info("Scheduler 已停止")


if __name__ == "__main__":
    main()
