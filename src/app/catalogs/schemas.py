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
        raise ValueError("code 须为 1–64 字符，以字母或数字开头，仅含字母、数字、下划线与连字符")
    return code


class ResourceCatalogCreate(BaseModel):
    code: str = Field(
        min_length=1, max_length=64, description="稳定业务编码，全局唯一，创建后尽量不要改"
    )
    name: str = Field(min_length=1, max_length=255, description="显示名称")
    parent_id: UUID | None = Field(default=None, description="父目录 ID；省略或空表示根节点")
    status: CatalogStatus = Field(
        default=CatalogStatus.ACTIVE, description="启用状态：ACTIVE 启用、DISABLED 停用"
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


class ResourceCatalogUpdate(BaseModel):
    """更新资源目录。未出现在请求体中的字段保持不变；`parent_id` 显式传 null 表示升为根。"""

    code: str | None = Field(
        default=None, min_length=1, max_length=64, description="新业务编码；省略不改"
    )
    name: str | None = Field(
        default=None, min_length=1, max_length=255, description="新显示名称；省略不改"
    )
    parent_id: UUID | None = Field(
        default=None, description="父目录：省略不改；UUID 改为该父级；null 升为根节点"
    )
    status: CatalogStatus | None = Field(
        default=None, description="启用状态：ACTIVE / DISABLED；省略不改"
    )
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


class ResourceCatalogResponse(BaseModel):
    id: UUID = Field(description="资源目录节点 ID")
    code: str = Field(description="稳定业务编码")
    name: str = Field(description="显示名称")
    parent_id: UUID | None = Field(description="父节点 ID；根节点为空")
    status: CatalogStatus = Field(description="启用状态：ACTIVE 启用、DISABLED 停用")
    sort_order: int = Field(description="同级排序")
    created_at: datetime = Field(description="创建时间（UTC，带时区）")
    updated_at: datetime = Field(description="最近更新时间（UTC，带时区）")


class ResourceCatalogTreeNode(BaseModel):
    """嵌套树节点；替代旧若依 TreeSelect DTO。"""

    id: UUID = Field(description="资源目录节点 ID")
    code: str = Field(description="稳定业务编码")
    name: str = Field(description="显示名称")
    status: CatalogStatus = Field(description="启用状态")
    sort_order: int = Field(description="同级排序")
    children: list["ResourceCatalogTreeNode"] = Field(default_factory=list, description="子节点")


class SatelliteCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, description="卫星业务编码，全局唯一")
    name: str = Field(min_length=1, max_length=255, description="卫星显示名称")
    status: CatalogStatus = Field(
        default=CatalogStatus.ACTIVE, description="启用状态：ACTIVE 启用、DISABLED 停用"
    )
    sort_order: int = Field(default=0, ge=0, le=1_000_000, description="排序，数值越小越靠前")

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
    code: str | None = Field(
        default=None, min_length=1, max_length=64, description="新业务编码；省略不改"
    )
    name: str | None = Field(
        default=None, min_length=1, max_length=255, description="新显示名称；省略不改"
    )
    status: CatalogStatus | None = Field(default=None, description="启用状态；省略不改")
    sort_order: int | None = Field(default=None, ge=0, le=1_000_000, description="排序；省略不改")

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
    id: UUID = Field(description="卫星 ID")
    code: str = Field(description="卫星业务编码")
    name: str = Field(description="卫星显示名称")
    status: CatalogStatus = Field(description="启用状态：ACTIVE 启用、DISABLED 停用")
    sort_order: int = Field(description="排序")
    created_at: datetime = Field(description="创建时间（UTC，带时区）")
    updated_at: datetime = Field(description="最近更新时间（UTC，带时区）")


class SensorCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, description="传感器业务编码，全局唯一")
    name: str = Field(min_length=1, max_length=255, description="传感器显示名称")
    satellite_id: UUID = Field(description="所属卫星 ID")
    status: CatalogStatus = Field(
        default=CatalogStatus.ACTIVE, description="启用状态：ACTIVE 启用、DISABLED 停用"
    )
    sort_order: int = Field(default=0, ge=0, le=1_000_000, description="排序，数值越小越靠前")

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
    code: str | None = Field(
        default=None, min_length=1, max_length=64, description="新业务编码；省略不改"
    )
    name: str | None = Field(
        default=None, min_length=1, max_length=255, description="新显示名称；省略不改"
    )
    satellite_id: UUID | None = Field(default=None, description="新所属卫星 ID；省略不改")
    status: CatalogStatus | None = Field(default=None, description="启用状态；省略不改")
    sort_order: int | None = Field(default=None, ge=0, le=1_000_000, description="排序；省略不改")

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
    id: UUID = Field(description="传感器 ID")
    code: str = Field(description="传感器业务编码")
    name: str = Field(description="传感器显示名称")
    satellite_id: UUID = Field(description="所属卫星 ID")
    status: CatalogStatus = Field(description="启用状态：ACTIVE 启用、DISABLED 停用")
    sort_order: int = Field(description="排序")
    created_at: datetime = Field(description="创建时间（UTC，带时区）")
    updated_at: datetime = Field(description="最近更新时间（UTC，带时区）")
