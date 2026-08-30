"""Job 执行租约心跳：Worker 执行期间后台续约，防止长步骤超过 TTL 后被误回收。

设计要点：
- 续约按 lease_token 条件更新：token 不匹配（已被恢复器回收、他人重新认领）时
  停止续约，避免僵尸执行者覆盖新持有者的租约；
- 单次续约失败只记日志不中断任务——下一次心跳会重试；连续失败超过 TTL 时恢复器
  会回收任务，当前执行者随后的状态写入按幂等语义处理；
- 心跳线程只做租约续约，不做任何业务状态转换。
"""

import logging
import threading
from datetime import timedelta
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from app.context import now_utc
from app.db import session_scope
from app.jobs.models import Job

logger = logging.getLogger(__name__)


class LeaseHeartbeat:
    """周期性为单个 RUNNING Job 续约的后台线程。"""

    def __init__(
        self,
        *,
        factory: Any,
        job_id: UUID,
        lease_token: UUID,
        interval_seconds: int,
        ttl_seconds: int,
    ) -> None:
        self._factory = factory
        self._job_id = job_id
        self._lease_token = lease_token
        self._interval_seconds = interval_seconds
        self._ttl_seconds = ttl_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="job-lease-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止心跳并等待线程退出；可安全重复调用。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(5.0, float(self._interval_seconds)))
            self._thread = None

    def _run(self) -> None:
        job_id = str(self._job_id)
        while not self._stop.wait(self._interval_seconds):
            try:
                renewed = self._renew_once()
            except Exception as exc:
                # 仅心跳这一处允许兜底捕获：续约失败不掩盖主流程，恢复器按租约过期兜底
                logger.warning(
                    "租约续约失败，将在下个周期重试",
                    extra={"job_id": job_id, "detail": str(exc)},
                )
                continue
            if not renewed:
                logger.warning(
                    "租约已不属于当前执行者（被回收或重新认领），停止续约",
                    extra={"job_id": job_id},
                )
                return

    def _renew_once(self) -> bool:
        """按 token 续约一次；返回 False 表示 token 已不匹配（失去执行权）。"""
        now = now_utc()
        with session_scope(self._factory) as session:
            result = session.execute(
                sa.update(Job)
                .where(Job.id == self._job_id, Job.lease_token == self._lease_token)
                .values(
                    heartbeat_at=now,
                    lease_expires_at=now + timedelta(seconds=self._ttl_seconds),
                )
            )
        return bool(result.rowcount)
