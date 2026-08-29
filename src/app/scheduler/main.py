"""独立 Scheduler 进程入口。

职责（架构边界）：评估到期监测计划，按数据库锁保证唯一发生标识，创建周期 Job 与
Outbox；停机恢复只补跑最近一次，其余错过周期标记 MISSED。该循环在 Stage 5 随
`monitoring_plan` 等表一并实装。

Stage 1 建立进程边界：连接数据库、等待表就绪、优雅停机。
"""

import logging
import signal
import time
from types import FrameType

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from app.db import create_engine
from app.logging import configure_logging
from app.settings import get_settings

logger = logging.getLogger("app.scheduler")

_PLAN_TABLE = "monitoring_plan"
_POLL_SECONDS = 10.0


class _StopFlag:
    def __init__(self) -> None:
        self.stopped = False

    def request_stop(self, signum: int, frame: FrameType | None) -> None:
        self.stopped = True


def _table_exists(engine: sa.Engine, table: str) -> bool:
    return sa.inspect(engine).has_table(table)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    stop = _StopFlag()
    signal.signal(signal.SIGTERM, stop.request_stop)
    signal.signal(signal.SIGINT, stop.request_stop)

    engine = create_engine(settings)
    logger.info("Scheduler 已启动", extra={"poll_seconds": _POLL_SECONDS})
    announced_ready = False
    try:
        while not stop.stopped:
            try:
                if _table_exists(engine, _PLAN_TABLE):
                    if not announced_ready:
                        logger.info(
                            "monitoring_plan 表已就绪，周期评估循环自 Stage 5 实装",
                            extra={"table": _PLAN_TABLE},
                        )
                        announced_ready = True
                else:
                    logger.info(
                        "等待 monitoring_plan 表（Stage 5 迁移创建）", extra={"table": _PLAN_TABLE}
                    )
            except SQLAlchemyError as exc:
                logger.warning("数据库暂不可达，稍后重试", extra={"detail": str(exc)})
            time.sleep(_POLL_SECONDS)
    finally:
        engine.dispose()
        logger.info("Scheduler 已停止")


if __name__ == "__main__":
    main()
