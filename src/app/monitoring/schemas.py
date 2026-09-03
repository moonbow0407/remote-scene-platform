"""监测计划、执行记录和本次选用的资产。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.monitoring.enums import OccurrenceTrigger, PlanStatus, RunStatus, ScheduleType

_POLYGON_EXAMPLE = {
    "type": "Polygon",
    "coordinates": [
        [
            [116.0, 39.0],
            [117.0, 39.0],
            [117.0, 40.0],
            [116.0, 40.0],
            [116.0, 39.0],
        ]
    ],
}


def _validate_boundary_is_object(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ValueError("boundary 必须是 GeoJSON 对象")
    return value


class PlanCreate(BaseModel):
    """新建监测计划。到点后按范围和分类自动挑选已处理完成的数据并执行。"""

    model_config = ConfigDict(title="创建监测计划")

    name: str = Field(min_length=1, max_length=255, description="计划名称")
    boundary: dict[str, Any] = Field(
        description="监测范围。必须是经纬度 GeoJSON 的 Polygon 或 MultiPolygon",
        examples=[_POLYGON_EXAMPLE],
    )
    schedule_type: ScheduleType = Field(description="怎么重复执行")
    schedule_expression: str = Field(
        min_length=1,
        max_length=256,
        description=(
            "INTERVAL 填时长：PT6H 每 6 小时，P1D 每天。"
            "RRULE 填重复规则，例如 FREQ=WEEKLY;BYDAY=MO 表示每周一"
        ),
        examples=["P1D"],
    )
    timezone: str = Field(
        min_length=1,
        max_length=64,
        description="时区名称，例如 Asia/Shanghai，用来解释当地的执行时刻",
        examples=["Asia/Shanghai"],
    )
    category_id: int | None = Field(
        default=None, description="只选这个分类的资产；不传表示不限分类"
    )
    ecological_parameter_ids: list[int] = Field(
        default_factory=list,
        description="只选这些生态参数细项对应分类下的资产；不要传大类。空数组表示不限",
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
    """改监测计划。没写的字段保持原值。分类传 null 表示不再限分类。"""

    model_config = ConfigDict(title="更新监测计划")

    name: str | None = Field(
        default=None, min_length=1, max_length=255, description="新名称；不传则不改"
    )
    boundary: dict[str, Any] | None = Field(
        default=None, description="新的监测范围（经纬度 GeoJSON）；不传则不改"
    )
    schedule_type: ScheduleType | None = Field(default=None, description="新的重复方式；不传则不改")
    schedule_expression: str | None = Field(
        default=None, min_length=1, max_length=256, description="新的重复表达式；不传则不改"
    )
    timezone: str | None = Field(
        default=None, min_length=1, max_length=64, description="新时区；不传则不改"
    )
    category_id: int | None = Field(
        default=None, description="分类编号；不传则不改，传 null 表示不再限分类"
    )
    ecological_parameter_ids: list[int] | None = Field(
        default=None, description="生态参数编号列表，传入则整表替换；不传则不改"
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
    """监测计划列表里的一行。"""

    model_config = ConfigDict(title="监测计划")

    id: int = Field(description="计划编号")
    name: str = Field(description="计划名称")
    status: PlanStatus = Field(description="是否还在按周期执行")
    schedule_type: ScheduleType = Field(description="重复方式")
    schedule_expression: str = Field(description="重复表达式")
    timezone: str = Field(description="时区名称")
    category_id: int | None = Field(description="限定的分类编号；不限分类为空")
    ecological_parameter_ids: list[int] = Field(
        description="限定的生态参数细项编号；空数组表示不限"
    )
    next_run_at: datetime | None = Field(description="下一次计划执行时间，UTC 且带时区")
    last_successful_run_at: datetime | None = Field(
        description="最近一次成功执行的时间。下次会从这之后挑选新数据"
    )
    created_at: datetime = Field(description="创建时间，UTC 且带时区")
    updated_at: datetime = Field(description="最近一次修改时间，UTC 且带时区")


class PlanDetailResponse(PlanSummaryResponse):
    """监测计划详情，含空间范围。"""

    model_config = ConfigDict(title="监测计划详情")

    boundary_geojson: dict[str, Any] | None = Field(
        default=None, description="监测范围，经纬度 GeoJSON"
    )


class RunResponse(BaseModel):
    """监测计划的一次执行。"""

    model_config = ConfigDict(title="监测执行")

    id: int = Field(description="这次执行的编号")
    plan_id: int = Field(description="所属计划编号")
    occurrence_id: int = Field(description="这次触发的编号。同一计划、同一计划时刻只有一个")
    scheduled_for: datetime = Field(description="计划中的执行时刻，UTC 且带时区")
    status: RunStatus = Field(description="这次执行的状态")
    window_anchor: datetime = Field(description="挑选数据的时间起点，一般是上次成功执行的时间")
    job_id: int | None = Field(description="对应的后台任务编号；还没开始时可为空")
    started_at: datetime | None = Field(description="实际开始时间；尚未开始为空")
    finished_at: datetime | None = Field(description="实际结束时间；尚未结束为空")
    diagnostics: dict[str, Any] | None = Field(description="失败或执行说明；正常时可为空")
    trigger: OccurrenceTrigger = Field(description="是到点自动执行，还是页面上手动点的")
    input_count: int = Field(description="这次选中的资产数量")
    created_at: datetime = Field(description="记录创建时间，UTC 且带时区")
    updated_at: datetime = Field(description="最近一次修改时间，UTC 且带时区")


class RunInputResponse(BaseModel):
    """这次执行选中的一份资产。名单在执行创建后不能改。"""

    model_config = ConfigDict(title="执行选用的资产")

    id: int = Field(description="这条记录的编号")
    run_id: int = Field(description="所属执行编号")
    asset_id: int = Field(description="选中的资产编号")
    created_at: datetime = Field(description="写入时间，UTC 且带时区")


class RunTransitionRequest(BaseModel):
    """标记执行失败时附带的说明。成功接口可以不传 body。"""

    model_config = ConfigDict(title="执行失败说明")

    detail: str | None = Field(
        default=None, max_length=2000, description="失败原因；标记成功时不必传"
    )
