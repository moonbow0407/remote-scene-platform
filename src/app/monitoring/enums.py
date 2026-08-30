"""监测模块领域枚举。

语义约定（与《阶段迁移实施方案》§8 一致）：
- MonitoringPlan 是长期配置（空间范围 + 目录/生态约束 + 调度周期），ACTIVE/PAUSED
  控制是否参与调度；软删除与 7 天恢复期属 Stage 6，不在本阶段引入；
- MonitoringOccurrence 是"一次计划触发"的稳定唯一标识，(plan_id, scheduled_for)
  数据库唯一，杜绝多实例/重复扫描/重启后为同一时刻重复创建执行；
- MonitoringRun 是某次 occurrence 的执行实例，输入快照一经创建不可变。
"""

from enum import StrEnum


class PlanStatus(StrEnum):
    """计划状态：ACTIVE 参与调度，PAUSED 暂停但不删除配置。"""

    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"


class ScheduleType(StrEnum):
    """调度类型：固定间隔（ISO 8601 duration 子集）或 RFC 5545 RRULE。"""

    INTERVAL = "INTERVAL"
    RRULE = "RRULE"


class OccurrenceTrigger(StrEnum):
    """occurrence 来源：调度器到期触发或人工手动触发。"""

    SCHEDULED = "SCHEDULED"
    MANUAL = "MANUAL"


class OccurrenceStatus(StrEnum):
    """occurrence 结果：已生成执行实例，或停机错过仅留审计记录。

    MISSED 不创建 MonitoringRun，也不产生任何任务——停机补跑只执行最近一次，
    其余周期记录为 MISSED 以避免任务风暴。
    """

    DISPATCHED = "DISPATCHED"
    MISSED = "MISSED"


class RunStatus(StrEnum):
    """监测执行状态。终态为 SUCCEEDED/FAILED；输入快照与状态无关地保持不可变。"""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
