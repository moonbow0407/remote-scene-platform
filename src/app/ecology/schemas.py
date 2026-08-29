"""生态模块 API 模型。"""

import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.ecology.enums import EcologicalParameterStatus

_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _validate_code(value: str) -> str:
    code = value.strip()
    if not _CODE_PATTERN.fullmatch(code):
        raise ValueError(
            "code 须为 1–64 字符，以字母或数字开头，仅含字母、数字、下划线与连字符"
        )
    return code


class EcologicalParameterCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, description="稳定业务编码，全局唯一")
    name: str = Field(min_length=1, max_length=255)
    parent_id: UUID | None = Field(default=None, description="父参数；根节点省略")
    status: EcologicalParameterStatus = Field(default=EcologicalParameterStatus.ACTIVE)
    sort_order: int = Field(default=0, ge=0, le=1_000_000)

    @field_validator("code")
    @classmethod
    def _code(cls, value: str) -> str:
        return _validate_code(value)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("name 不能为空")
        return name


class EcologicalParameterUpdate(BaseModel):
    """未出现字段保持不变；`parent_id` 显式 null 表示升为根。"""

    code: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    parent_id: UUID | None = None
    status: EcologicalParameterStatus | None = None
    sort_order: int | None = Field(default=None, ge=0, le=1_000_000)

    @field_validator("code")
    @classmethod
    def _code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_code(value)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        name = value.strip()
        if not name:
            raise ValueError("name 不能为空")
        return name


class EcologicalParameterResponse(BaseModel):
    id: UUID
    code: str
    name: str
    parent_id: UUID | None
    status: EcologicalParameterStatus
    sort_order: int
    created_at: datetime
    updated_at: datetime


class EcologicalParameterTreeNode(BaseModel):
    id: UUID
    code: str
    name: str
    status: EcologicalParameterStatus
    sort_order: int
    children: list["EcologicalParameterTreeNode"] = Field(default_factory=list)


class MappingCreate(BaseModel):
    ecological_parameter_id: UUID
    resource_catalog_id: UUID


class MappingBatchCreate(BaseModel):
    items: list[MappingCreate] = Field(default_factory=list, description="映射条目；允许空列表")


class MappingResponse(BaseModel):
    id: UUID
    ecological_parameter_id: UUID
    resource_catalog_id: UUID
    created_at: datetime


class MappingBatchResponse(BaseModel):
    """批量结果：已存在关系幂等保留，不报冲突。"""

    created: list[MappingResponse]
    existing: list[MappingResponse]
    created_count: int
    existing_count: int
