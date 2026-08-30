"""过期资产与 MinIO 对象异步清理循环。"""

from __future__ import annotations

import logging
import signal
import time
from types import FrameType

from sqlalchemy.exc import SQLAlchemyError

from app.assets.lifecycle import AssetLifecycleService, ObjectCleanupService
from app.context import now_utc
from app.db import create_engine, make_session_factory, session_scope
from app.logging import configure_logging
from app.model_registry import *  # noqa: F403 迁移/外键目标模型全部注册
from app.settings import get_settings
from app.uploads.minio import MinioAdapter

logger = logging.getLogger("app.cleanup")


class _StopFlag:
    def __init__(self) -> None:
        self.stopped = False

    def request_stop(self, signum: int, frame: FrameType | None) -> None:
        self.stopped = True


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    stop = _StopFlag()
    signal.signal(signal.SIGTERM, stop.request_stop)
    signal.signal(signal.SIGINT, stop.request_stop)
    engine = create_engine(settings)
    factory = make_session_factory(engine)
    minio = MinioAdapter(settings)
    logger.info(
        "生命周期清理器已启动",
        extra={
            "poll_seconds": settings.cleanup_poll_seconds,
            "batch_size": settings.cleanup_batch_size,
        },
    )
    try:
        while not stop.stopped:
            did_work = False
            now = now_utc()
            try:
                with session_scope(factory) as session:
                    due_ids = AssetLifecycleService(session).due_asset_ids(
                        now=now, limit=settings.cleanup_batch_size
                    )
                for asset_id in due_ids:
                    try:
                        with session_scope(factory) as session:
                            if AssetLifecycleService(session).purge_asset(asset_id, now=now):
                                did_work = True
                    except Exception as exc:
                        logger.exception(
                            "过期资产清理失败，将退避重试",
                            extra={"asset_id": str(asset_id)},
                        )
                        with session_scope(factory) as session:
                            AssetLifecycleService(session).record_purge_failure(
                                asset_id, str(exc), now=now
                            )

                with session_scope(factory) as session:
                    tasks = ObjectCleanupService(session, minio).claim_due(
                        now=now, limit=settings.cleanup_batch_size
                    )
                    for task in tasks:
                        deleted = ObjectCleanupService(session, minio).execute(task, now=now)
                        did_work = deleted or did_work
                if not did_work:
                    time.sleep(settings.cleanup_poll_seconds)
            except SQLAlchemyError as exc:
                logger.warning("清理器数据库暂不可达，稍后重试", extra={"detail": str(exc)})
                time.sleep(settings.cleanup_poll_seconds)
    finally:
        engine.dispose()
        logger.info("生命周期清理器已停止")


if __name__ == "__main__":
    main()
