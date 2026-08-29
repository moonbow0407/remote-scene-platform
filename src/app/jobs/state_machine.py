"""Job 状态机：唯一合法转换表。

状态转换必须经 JobService 执行（校验转换合法性并追加 JobEvent）；
任意直接修改 job.status 都绕过了审计与并发保护，属违规。
"""

from app.jobs.enums import JobStatus

# 合法状态转换：终态（SUCCEEDED/FAILED/CANCELLED/MISSED）无出边
ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    # Broker 可能在 Dispatcher 回写 QUEUED 前完成投递；Worker 可直接原子认领 PENDING。
    JobStatus.PENDING: frozenset({JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.CANCELLED}),
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED}),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.RETRYING,
            JobStatus.NEEDS_INPUT,
            JobStatus.CANCEL_REQUESTED,
        }
    ),
    JobStatus.RETRYING: frozenset(
        {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.FAILED, JobStatus.CANCELLED}
    ),
    JobStatus.NEEDS_INPUT: frozenset({JobStatus.QUEUED, JobStatus.CANCELLED}),
    JobStatus.CANCEL_REQUESTED: frozenset(
        {JobStatus.CANCELLED, JobStatus.SUCCEEDED, JobStatus.FAILED}
    ),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
    JobStatus.MISSED: frozenset(),
}


def is_transition_allowed(current: JobStatus, target: JobStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]
