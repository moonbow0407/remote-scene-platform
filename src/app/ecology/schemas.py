"""生态模块 API 模型。"""

import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.ecology.enums import EcologicalParameterStatus

_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _validate_code(value: str) -> str:
    code = value.strip()
    if not _CODE_PATTERN.fullmatch(code):
        raise ValueError("code 须为 1–64 字符，以字母或数字开头，仅含字母、数字、下划线与连字符")
    return code


class EcologicalParameterCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, description="稳定业务编码，全局唯一")
    name: str = Field(min_length=1, max_length=255, description="生态参数显示名称")
    parent_id: int | None = Field(default=None, description="父参数 ID；省略表示根节点")
    status: EcologicalParameterStatus = Field(
        default=EcologicalParameterStatus.ACTIVE,
        description="启用状态：ACTIVE 启用、DISABLED 停用",
    )
    sort_order: int = Field(default=0, ge=0, le=1_000_000, description="同级排序，数值越小越靠前")

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

    code: str | None = Field(
        default=None, min_length=1, max_length=64, description="新业务编码；省略不改"
    )
    name: str | None = Field(
        default=None, min_length=1, max_length=255, description="新显示名称；省略不改"
    )
    parent_id: int | None = Field(
        default=None, description="父参数：省略不改；UUID 改为该父级；null 升为根节点"
    )
    status: EcologicalParameterStatus | None = Field(default=None, description="启用状态；省略不改")
    sort_order: int | None = Field(
        default=None, ge=0, le=1_000_000, description="同级排序；省略不改"
    )

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
    id: int = Field(description="生态参数 ID")
    code: str = Field(description="稳定业务编码")
    name: str = Field(description="显示名称")
    parent_id: int | None = Field(description="父参数 ID；根节点为空")
    status: EcologicalParameterStatus = Field(description="启用状态：ACTIVE 启用、DISABLED 停用")
    sort_order: int = Field(description="同级排序")
    created_at: datetime = Field(description="创建时间（UTC，带时区）")
    updated_at: datetime = Field(description="最近更新时间（UTC，带时区）")


class EcologicalParameterTreeNode(BaseModel):
    id: int = Field(description="生态参数 ID")
    code: str = Field(description="稳定业务编码")
    name: str = Field(description="显示名称")
    status: EcologicalParameterStatus = Field(description="启用状态")
    sort_order: int = Field(description="同级排序")
    children: list["EcologicalParameterTreeNode"] = Field(
        default_factory=list, description="子节点"
    )


class MappingCreate(BaseModel):
    ecological_parameter_id: int = Field(description="生态参数 ID")
    category_id: int = Field(description="分类 ID")


class MappingBatchCreate(BaseModel):
    items: list[MappingCreate] = Field(
        default_factory=list, description="待创建的映射条目；允许空列表；已存在的关系会幂等保留"
    )


class MappingResponse(BaseModel):
    id: int = Field(description="映射 ID")
    ecological_parameter_id: int = Field(description="生态参数 ID")
    category_id: int = Field(description="分类 ID")
    created_at: datetime = Field(description="创建时间（UTC，带时区）")


class MappingBatchResponse(BaseModel):
    """批量结果：已存在关系幂等保留，不报冲突。"""

    created: list[MappingResponse] = Field(description="本次新创建的映射")
    existing: list[MappingResponse] = Field(description="请求中已存在、被幂等保留的映射")
    created_count: int = Field(description="新创建条数")
    existing_count: int = Field(description="已存在条数")
