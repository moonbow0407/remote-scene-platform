"""监测模块 API 模型。

boundary 以 GeoJSON（EPSG:4326 Polygon/MultiPolygon）进出；合法性由
`app.assets.geometry.geojson_to_wkt` 校验（复用项目既有几何校验体系），存储侧
统一归一化为 MULTIPOLYGON。
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.monitoring.enums import OccurrenceTrigger, PlanStatus, RunStatus, ScheduleType


def _validate_boundary_is_object(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ValueError("boundary 必须是 GeoJSON 对象")
    return value


class PlanCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255, description="监测计划名称")
    boundary: dict[str, Any] = Field(
        description="监测空间范围，必须是 EPSG:4326 GeoJSON Polygon 或 MultiPolygon"
    )
    schedule_type: ScheduleType = Field(description="调度类型：INTERVAL 固定间隔、RRULE 重复规则")
    schedule_expression: str = Field(
        min_length=1,
        max_length=256,
        description=(
            "INTERVAL 填 ISO 8601 时长（如 PT6H、P1D）；RRULE 填 RFC 5545 表达式（不含 DTSTART）"
        ),
    )
    timezone: str = Field(
        min_length=1, max_length=64, description="IANA 时区名，如 Asia/Shanghai，用于解释调度周期"
    )
    resource_catalog_id: UUID | None = Field(
        default=None, description="资源目录约束；省略表示不限目录"
    )
    ecological_parameter_ids: list[UUID] = Field(
        default_factory=list, description="生态参数约束；空列表表示不限生态参数"
    )

    @field_validator("boundary")
    @classmethod
    def _boundary(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_boundary_is_object(value)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("name 不能为空")
        return name

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str) -> str:
        tz = value.strip()
        if not tz:
            raise ValueError("timezone 不能为空")
        return tz


class PlanUpdate(BaseModel):
    """部分更新：未出现字段保持不变。

    `resource_catalog_id` 显式 null 表示清除目录约束；`boundary`、调度三字段、
    `ecological_parameter_ids` 出现即整体替换（列表不做增量合并）。
    """

    name: str | None = Field(
        default=None, min_length=1, max_length=255, description="新名称；省略不改"
    )
    boundary: dict[str, Any] | None = Field(
        default=None, description="新空间范围（EPSG:4326 GeoJSON）；省略不改"
    )
    schedule_type: ScheduleType | None = Field(default=None, description="新调度类型；省略不改")
    schedule_expression: str | None = Field(
        default=None, min_length=1, max_length=256, description="新调度表达式；省略不改"
    )
    timezone: str | None = Field(
        default=None, min_length=1, max_length=64, description="新时区；省略不改"
    )
    resource_catalog_id: UUID | None = Field(
        default=None, description="资源目录：省略不改；UUID 改为该目录；null 清除约束"
    )
    ecological_parameter_ids: list[UUID] | None = Field(
        default=None, description="生态参数列表，出现则整体替换；省略不改"
    )

    @field_validator("boundary")
    @classmethod
    def _boundary(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return _validate_boundary_is_object(value)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        name = value.strip()
        if not name:
            raise ValueError("name 不能为空")
        return name

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        tz = value.strip()
        if not tz:
            raise ValueError("timezone 不能为空")
        return tz


class PlanSummaryResponse(BaseModel):
    id: UUID = Field(description="监测计划 ID")
    name: str = Field(description="计划名称")
    status: PlanStatus = Field(description="计划状态：ACTIVE 参与调度、PAUSED 已暂停")
    schedule_type: ScheduleType = Field(description="调度类型：INTERVAL / RRULE")
    schedule_expression: str = Field(description="调度表达式")
    timezone: str = Field(description="IANA 时区名")
    resource_catalog_id: UUID | None = Field(description="资源目录约束；空表示不限")
    ecological_parameter_ids: list[UUID] = Field(description="生态参数约束；空列表表示不限")
    next_run_at: datetime | None = Field(description="下一次计划触发时间（UTC，带时区）")
    last_successful_run_at: datetime | None = Field(
        description="最近一次成功执行时间（UTC，带时区）；增量窗口以此为锚点"
    )
    created_at: datetime = Field(description="创建时间（UTC，带时区）")
    updated_at: datetime = Field(description="最近更新时间（UTC，带时区）")


class PlanDetailResponse(PlanSummaryResponse):
    boundary_geojson: dict[str, Any] | None = Field(
        default=None, description="监测空间范围，EPSG:4326 GeoJSON"
    )


class RunResponse(BaseModel):
    id: UUID = Field(description="监测执行 ID")
    plan_id: UUID = Field(description="所属计划 ID")
    occurrence_id: UUID = Field(description="本次触发的稳定标识（计划+计划时刻唯一）")
    scheduled_for: datetime = Field(description="计划触发时刻（UTC，带时区）")
    status: RunStatus = Field(
        description="执行状态：PENDING 待执行、RUNNING 执行中、SUCCEEDED 成功、FAILED 失败"
    )
    window_anchor: datetime = Field(description="增量时间窗锚点，通常为上次成功执行时间")
    job_id: UUID | None = Field(description="对应的处理任务 ID；尚未派发可为空")
    started_at: datetime | None = Field(description="实际开始时间（UTC，带时区）")
    finished_at: datetime | None = Field(description="实际结束时间（UTC，带时区）")
    diagnostics: dict[str, Any] | None = Field(description="失败或执行诊断")
    trigger: OccurrenceTrigger = Field(description="触发来源：SCHEDULED 调度、MANUAL 手动")
    input_count: int = Field(description="冻结的输入资产版本数量")
    created_at: datetime = Field(description="创建时间（UTC，带时区）")
    updated_at: datetime = Field(description="最近更新时间（UTC，带时区）")


class RunInputResponse(BaseModel):
    id: UUID = Field(description="输入快照条目 ID")
    run_id: UUID = Field(description="所属监测执行 ID")
    asset_id: UUID = Field(description="逻辑资产 ID")
    asset_version_id: UUID = Field(description="冻结的具体资产版本 ID，不会随后续新版本变化")
    created_at: datetime = Field(description="快照写入时间（UTC，带时区）")


class RunTransitionRequest(BaseModel):
    """Run 状态推进请求体；供监测执行方（未来 Worker/运维接缝）调用。"""

    detail: str | None = Field(
        default=None, max_length=2000, description="失败诊断说明；成功接口可省略"
    )
