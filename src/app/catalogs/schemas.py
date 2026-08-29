"""目录模块 API 模型。"""

import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.catalogs.enums import CatalogStatus

# 稳定业务编码：字母数字、下划线、连字符；禁止空白与路径分隔符
_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _validate_code(value: str) -> str:
    code = value.strip()
    if not _CODE_PATTERN.fullmatch(code):
        raise ValueError(
            "code 须为 1–64 字符，以字母或数字开头，仅含字母、数字、下划线与连字符"
        )
    return code


class ResourceCatalogCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, description="稳定业务编码，全局唯一")
    name: str = Field(min_length=1, max_length=255, description="显示名称")
    parent_id: UUID | None = Field(default=None, description="父目录；根节点省略")
    status: CatalogStatus = Field(default=CatalogStatus.ACTIVE)
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


class ResourceCatalogUpdate(BaseModel):
    """更新资源目录。未出现在请求体中的字段保持不变；`parent_id` 显式传 null 表示升为根。"""

    code: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    parent_id: UUID | None = Field(
        default=None, description="省略=不改；UUID=新父级；null=升为根节点"
    )
    status: CatalogStatus | None = None
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


class ResourceCatalogResponse(BaseModel):
    id: UUID
    code: str
    name: str
    parent_id: UUID | None
    status: CatalogStatus
    sort_order: int
    created_at: datetime
    updated_at: datetime


class ResourceCatalogTreeNode(BaseModel):
    """嵌套树节点；替代旧若依 TreeSelect DTO。"""

    id: UUID
    code: str
    name: str
    status: CatalogStatus
    sort_order: int
    children: list["ResourceCatalogTreeNode"] = Field(default_factory=list)


class SatelliteCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    status: CatalogStatus = Field(default=CatalogStatus.ACTIVE)
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


class SatelliteUpdate(BaseModel):
    code: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    status: CatalogStatus | None = None
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


class SatelliteResponse(BaseModel):
    id: UUID
    code: str
    name: str
    status: CatalogStatus
    sort_order: int
    created_at: datetime
    updated_at: datetime


class SensorCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    satellite_id: UUID = Field(description="所属卫星主键")
    status: CatalogStatus = Field(default=CatalogStatus.ACTIVE)
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


class SensorUpdate(BaseModel):
    code: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    satellite_id: UUID | None = None
    status: CatalogStatus | None = None
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


class SensorResponse(BaseModel):
    id: UUID
    code: str
    name: str
    satellite_id: UUID
    status: CatalogStatus
    sort_order: int
    created_at: datetime
    updated_at: datetime
