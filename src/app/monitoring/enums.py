"""监测模块领域枚举。

语义约定：
- MonitoringPlan 是长期配置（空间范围 + 目录/生态约束 + 调度周期），ACTIVE/PAUSED
  控制是否参与调度；计划删除是物理删除；
- MonitoringOccurrence 是"一次计划触发"的稳定唯一标识，(plan_id, scheduled_for)
  数据库唯一，杜绝多实例/重复扫描/重启后为同一时刻重复创建执行；
- MonitoringRun 是某次 occurrence 的执行实例，输入快照一经创建不可变。
"""

from enum import StrEnum

from app.schema_docs import enum_docs


@enum_docs("计划状态", "ACTIVE：按周期自动执行；PAUSED：暂停，配置保留。")
class PlanStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"


@enum_docs(
    "调度类型",
    "INTERVAL：固定间隔，例如每 6 小时、每天；RRULE：按星期几等规则重复。",
)
class ScheduleType(StrEnum):
    INTERVAL = "INTERVAL"
    RRULE = "RRULE"


@enum_docs("触发来源", "SCHEDULED：到点自动执行；MANUAL：页面上手动点一次。")
class OccurrenceTrigger(StrEnum):
    SCHEDULED = "SCHEDULED"
    MANUAL = "MANUAL"


class OccurrenceStatus(StrEnum):
    """occurrence 结果：已生成执行实例，或停机错过仅留审计记录。

    MISSED 不创建 MonitoringRun，也不产生任何任务——停机补跑只执行最近一次，
    其余周期记录为 MISSED 以避免任务风暴。
    """

    DISPATCHED = "DISPATCHED"
    MISSED = "MISSED"


@enum_docs(
    "执行状态",
    "PENDING：尚未开始；RUNNING：正在执行；SUCCEEDED：成功；FAILED：失败。",
)
class RunStatus(StrEnum):
    """一次监测执行的状态。"""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
