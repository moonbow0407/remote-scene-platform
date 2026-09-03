"""数据源字典接口。"""

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.data_sources.enums import DataSourceStatus
from app.data_sources.seed_data import CODE_PATTERN, kind_of_code
from app.imagery.enums import RecordKind


class DataSourceCreate(BaseModel):
    model_config = ConfigDict(title="创建数据源")

    code: str = Field(
        min_length=6, max_length=6, description="六位编号，0001xx 卫星、0002xx 无人机"
    )
    name: str = Field(min_length=1, max_length=255, description="显示名称")
    kind: RecordKind | None = Field(
        default=None, description="SATELLITE 或 UAV。不传则按编号前四位判断"
    )
    status: DataSourceStatus = Field(default=DataSourceStatus.ACTIVE, description="是否启用")

    @field_validator("code")
    @classmethod
    def _code(cls, value: str) -> str:
        code = value.strip()
        if not re.fullmatch(CODE_PATTERN, code):
            raise ValueError("code 须为 6 位数字，例如 000114")
        return code

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("name 不能为空")
        return name


class DataSourceUpdate(BaseModel):
    model_config = ConfigDict(title="更新数据源")

    name: str | None = Field(default=None, min_length=1, max_length=255, description="新名称")
    status: DataSourceStatus | None = Field(default=None, description="启用状态")

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        name = value.strip()
        if not name:
            raise ValueError("name 不能为空")
        return name


class DataSourceResponse(BaseModel):
    model_config = ConfigDict(title="数据源")

    id: int = Field(description="数据源编号")
    code: str = Field(description="六位产品型号，例如 000114")
    name: str = Field(description="显示名称，例如 哨兵二号")
    kind: RecordKind = Field(description="SATELLITE 或 UAV")
    status: DataSourceStatus = Field(description="是否启用")
    created_at: datetime
    updated_at: datetime


def resolve_kind(code: str, kind: RecordKind | None) -> RecordKind:
    inferred = kind_of_code(code)
    if kind is None:
        return inferred
    if kind is not inferred:
        raise ValueError(f"编号 {code} 对应 {inferred}，不能标成 {kind}")
    return kind
