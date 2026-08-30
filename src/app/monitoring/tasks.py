"""Celery 任务定义：监测执行（monitoring.execute_run）。

错误分类与可靠性模式与入库任务（processing.tasks）一致：瞬时错误经 Outbox
退避重投，确定性错误直接失败；执行权以 Job 租约为准。执行核心见
monitoring.execution。
"""

from celery import Task

from app.monitoring.execution import execute_monitoring_run
from app.worker.celery_app import celery


@celery.task(name="monitoring.execute_run", bind=True, ignore_result=True)
def execute_run(self: Task, job_id: str) -> None:
    execute_monitoring_run(job_id)
