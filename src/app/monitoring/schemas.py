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
    name: str = Field(min_length=1, max_length=255)
    boundary: dict[str, Any] = Field(description="EPSG:4326 GeoJSON Polygon/MultiPolygon")
    schedule_type: ScheduleType
    schedule_expression: str = Field(
        min_length=1,
        max_length=256,
        description="INTERVAL：ISO 8601 duration（PT6H/P1D）；RRULE：RFC 5545 表达式（无 DTSTART）",
    )
    timezone: str = Field(min_length=1, max_length=64, description="IANA 时区名，如 Asia/Shanghai")
    resource_catalog_id: UUID | None = Field(default=None, description="资源目录约束；空=不限")
    ecological_parameter_ids: list[UUID] = Field(
        default_factory=list, description="生态参数约束；空=不限"
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

    name: str | None = Field(default=None, min_length=1, max_length=255)
    boundary: dict[str, Any] | None = None
    schedule_type: ScheduleType | None = None
    schedule_expression: str | None = Field(default=None, min_length=1, max_length=256)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    resource_catalog_id: UUID | None = None
    ecological_parameter_ids: list[UUID] | None = None

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
    id: UUID
    name: str
    status: PlanStatus
    schedule_type: ScheduleType
    schedule_expression: str
    timezone: str
    resource_catalog_id: UUID | None
    ecological_parameter_ids: list[UUID]
    next_run_at: datetime | None
    last_successful_run_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PlanDetailResponse(PlanSummaryResponse):
    boundary_geojson: dict[str, Any] | None = None


class RunResponse(BaseModel):
    id: UUID
    plan_id: UUID
    occurrence_id: UUID
    scheduled_for: datetime
    status: RunStatus
    window_anchor: datetime
    job_id: UUID | None
    started_at: datetime | None
    finished_at: datetime | None
    diagnostics: dict[str, Any] | None
    trigger: OccurrenceTrigger
    input_count: int
    created_at: datetime
    updated_at: datetime


class RunInputResponse(BaseModel):
    id: UUID
    run_id: UUID
    asset_id: UUID
    asset_version_id: UUID
    created_at: datetime


class RunTransitionRequest(BaseModel):
    """Run 状态推进请求体；供监测执行方（未来 Worker/运维接缝）调用。"""

    detail: str | None = Field(default=None, max_length=2000, description="失败诊断说明")
