"""恢复器主循环：扫描租约过期的 RUNNING Job 并回收重投。

与 Dispatcher/Scheduler 同构：连接数据库、优雅停机、数据库暂不可达时退避重试。
扫描间隔显著大于心跳间隔即可；恢复动作见 app.jobs.recovery。
"""

import logging
import signal
import time
from types import FrameType

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from app.db import create_engine, make_session_factory, session_scope
from app.jobs.recovery import recover_expired_leases
from app.logging import configure_logging
from app.model_registry import *  # noqa: F403  确保外键目标表全部注册
from app.settings import get_settings

logger = logging.getLogger("app.recovery")

_POLL_SECONDS = 30.0
_BATCH_SIZE = 50


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
    factory = make_session_factory(engine)
    logger.info("Job 恢复器已启动", extra={"poll_seconds": _POLL_SECONDS, "batch": _BATCH_SIZE})
    try:
        while not stop.stopped:
            try:
                if not _table_exists(engine, "job"):
                    time.sleep(_POLL_SECONDS)
                    continue
                with session_scope(factory) as session:
                    recovered = recover_expired_leases(session, batch_size=_BATCH_SIZE)
                if recovered:
                    logger.warning("本轮回收失联任务", extra={"count": len(recovered)})
                time.sleep(_POLL_SECONDS)
            except SQLAlchemyError as exc:
                logger.warning("数据库暂不可达，稍后重试", extra={"detail": str(exc)})
                time.sleep(_POLL_SECONDS)
    finally:
        engine.dispose()
        logger.info("Job 恢复器已停止")


if __name__ == "__main__":
    main()
