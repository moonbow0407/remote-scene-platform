"""Outbox Dispatcher：认领 Outbox 事件并至少一次投递到 RabbitMQ。

可靠性设计（AGENTS.md §3.5）：
- 认领使用 FOR UPDATE SKIP LOCKED + 认领 TTL，多 Dispatcher 并发安全；
- 陈旧认领（Dispatcher 崩溃后超时）自动回收重投；
- 发布失败按指数退避回退为 PENDING；成功后 Job 推进为 QUEUED；
- 至少一次投递允许重复消息，Worker 按 Job 幂等执行。

发布使用 celery.send_task（celery 为纯 Python 依赖，不引入 GDAL/科学栈），
协议与路由由 Celery 保证；Outbox 仍是投递可靠性的权威来源。
"""

import logging
import signal
import time
from types import FrameType

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from app.db import create_engine, make_session_factory, session_scope
from app.jobs.models import OutboxEvent
from app.jobs.service import OutboxRepository
from app.logging import configure_logging
from app.model_registry import *  # noqa: F403  确保外键目标表全部注册
from app.settings import get_settings
from app.worker.celery_app import celery

logger = logging.getLogger("app.dispatcher")

_OUTBOX_TABLE = "outbox_event"
_POLL_SECONDS = 1.0
_CLAIM_TTL_SECONDS = 60
_BATCH_SIZE = 20


class _StopFlag:
    def __init__(self) -> None:
        self.stopped = False

    def request_stop(self, signum: int, frame: FrameType | None) -> None:
        self.stopped = True


def _table_exists(engine: sa.Engine, table: str) -> bool:
    return sa.inspect(engine).has_table(table)


def _publish(pending: list[OutboxEvent]) -> tuple[list[OutboxEvent], list[OutboxEvent]]:
    """发布一批事件；返回 (成功, 失败)。"""
    published: list[OutboxEvent] = []
    failed: list[OutboxEvent] = []
    for event in pending:
        payload = event.payload
        task_name = str(payload["task"])
        task_args = [str(a) for a in payload.get("args", [])]
        try:
            celery.send_task(
                task_name,
                args=task_args,
                task_id=str(event.id),
                queue="geo",
                ignore_result=True,
            )
            published.append(event)
        except Exception as exc:
            logger.warning(
                "Outbox 事件发布失败，将按指数退避重试",
                extra={"event_id": str(event.id), "detail": str(exc)},
            )
            failed.append(event)
    return published, failed


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    stop = _StopFlag()
    signal.signal(signal.SIGTERM, stop.request_stop)
    signal.signal(signal.SIGINT, stop.request_stop)

    engine = create_engine(settings)
    factory = make_session_factory(engine)
    logger.info(
        "Dispatcher 已启动", extra={"poll_seconds": _POLL_SECONDS, "batch_size": _BATCH_SIZE}
    )
    try:
        while not stop.stopped:
            try:
                if not _table_exists(engine, _OUTBOX_TABLE):
                    logger.info(
                        "等待 outbox_event 表（Stage 2 迁移创建）", extra={"table": _OUTBOX_TABLE}
                    )
                    time.sleep(_POLL_SECONDS)
                    continue

                with session_scope(factory) as session:
                    pending = OutboxRepository(session).claim_batch(
                        batch_size=_BATCH_SIZE, claim_ttl_seconds=_CLAIM_TTL_SECONDS
                    )
                if not pending:
                    time.sleep(_POLL_SECONDS)
                    continue

                published, failed = _publish(pending)

                with session_scope(factory) as session:
                    repo = OutboxRepository(session)
                    job_ids: list[int] = []
                    for event in published:
                        # 重新读取当前行（上一事务已提交认领状态）
                        row = session.get(OutboxEvent, event.id)
                        if row is not None:
                            repo.mark_published(row)
                            job_ids.append(row.aggregate_id)
                    repo.set_job_queued_after_publish(job_ids)

                if failed:
                    with session_scope(factory) as session:
                        repo = OutboxRepository(session)
                        for event in failed:
                            row = session.get(OutboxEvent, event.id)
                            if row is not None:
                                repo.mark_retry(row)

                logger.info(
                    "Outbox 批次处理完成",
                    extra={"published": len(published), "failed": len(failed)},
                )
            except SQLAlchemyError as exc:
                logger.warning("数据库暂不可达，稍后重试", extra={"detail": str(exc)})
                time.sleep(_POLL_SECONDS)
    finally:
        engine.dispose()
        logger.info("Dispatcher 已停止")


if __name__ == "__main__":
    main()
