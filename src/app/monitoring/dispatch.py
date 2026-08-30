"""监测执行的真实派发器：Run → Job(MONITORING_RUN) + Outbox（同事务）。

模块边界（对齐《总体架构》与《阶段迁移实施方案》§7）：监测模块决定
"什么时候执行、执行什么"（调度、occurrence 唯一性、增量选择、快照冻结），
Job 基础设施负责"怎么可靠执行"（状态机、Outbox、租约、重试）。本类是两个
模块唯一的衔接点：在同一次数据库事务中经公共接口 `JobService.create_job_with_outbox`
创建 MONITORING_RUN 任务与投递事件；其后由 Outbox Dispatcher 投递到 RabbitMQ，
Geo Worker 中的 `monitoring.execute_run` 任务认领执行。

- 禁止绕过 Outbox 直接 send_task / 发布 RabbitMQ 消息（双写无法原子）；
- 幂等不在此处实现：occurrence 的 (plan_id, scheduled_for) 唯一约束保证同一
  计划时刻至多进入一次 Run 创建，Job 随之至多创建一次；
- payload 只携带定位信息（run_id/plan_id/输入数量），输入快照的权威数据在
  monitoring_run_input，执行方从数据库读取，避免两处快照漂移。
"""

from uuid import UUID

from sqlalchemy.orm import Session

from app.jobs.enums import JobType
from app.jobs.service import JobService
from app.monitoring.models import MonitoringRun


class JobRunDispatcher:
    """RunDispatcher 的生产实现：创建 Job 与 Outbox 事件，并回写 run.job_id。"""

    def dispatch(
        self, session: Session, run: MonitoringRun, input_version_ids: list[UUID]
    ) -> UUID | None:
        job, _event = JobService(session).create_job_with_outbox(
            job_type=JobType.MONITORING_RUN,
            payload={
                "run_id": str(run.id),
                "plan_id": str(run.plan_id),
                "input_count": len(input_version_ids),
            },
        )
        return job.id
