"""生态参数及其与分类的对应关系。"""

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.ecology.enums import EcologicalParameterStatus

_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _validate_code(value: str) -> str:
    code = value.strip()
    if not _CODE_PATTERN.fullmatch(code):
        raise ValueError("code 须为 1–64 字符，以字母或数字开头，仅含字母、数字、下划线与连字符")
    return code


class EcologicalParameterCreate(BaseModel):
    """新建一个生态参数。可以挂在另一个参数下面，形成树。"""

    model_config = ConfigDict(title="创建生态参数")

    code: str = Field(
        min_length=1,
        max_length=64,
        description="业务编码，全局不能重复，例如 NDVI",
        examples=["NDVI"],
    )
    name: str = Field(min_length=1, max_length=255, description="显示名称")
    parent_id: int | None = Field(default=None, description="父参数编号。不传表示这是顶层参数")
    status: EcologicalParameterStatus = Field(
        default=EcologicalParameterStatus.ACTIVE, description="是否启用"
    )
    sort_order: int = Field(
        default=0, ge=0, le=1_000_000, description="同一层里的排序，数字越小越靠前"
    )

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
    """改生态参数。没写的字段保持原值。parent_id 传 null 表示升到顶层。"""

    model_config = ConfigDict(title="更新生态参数")

    code: str | None = Field(
        default=None, min_length=1, max_length=64, description="新编码；不传则不改"
    )
    name: str | None = Field(
        default=None, min_length=1, max_length=255, description="新名称；不传则不改"
    )
    parent_id: int | None = Field(
        default=None, description="父参数编号；不传则不改，传 null 表示升到顶层"
    )
    status: EcologicalParameterStatus | None = Field(
        default=None, description="启用状态；不传则不改"
    )
    sort_order: int | None = Field(
        default=None, ge=0, le=1_000_000, description="同层排序；不传则不改"
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
    """一个生态参数。"""

    model_config = ConfigDict(title="生态参数")

    id: int = Field(description="生态参数编号")
    code: str = Field(description="业务编码")
    name: str = Field(description="显示名称")
    parent_id: int | None = Field(description="父参数编号；顶层参数为空")
    status: EcologicalParameterStatus = Field(description="是否启用")
    sort_order: int = Field(description="同一层里的排序")
    created_at: datetime = Field(description="创建时间，UTC 且带时区")
    updated_at: datetime = Field(description="最近一次修改时间，UTC 且带时区")


class EcologicalParameterTreeNode(BaseModel):
    """生态参数树的一个节点。"""

    model_config = ConfigDict(title="生态参数树节点")

    id: int = Field(description="生态参数编号")
    code: str = Field(description="业务编码")
    name: str = Field(description="显示名称")
    status: EcologicalParameterStatus = Field(description="是否启用")
    sort_order: int = Field(description="同一层里的排序")
    children: list["EcologicalParameterTreeNode"] = Field(
        default_factory=list, description="子节点，没有则为空数组"
    )


class MappingCreate(BaseModel):
    """把一个生态参数对应到一个分类。"""

    model_config = ConfigDict(title="创建生态映射")

    ecological_parameter_id: int = Field(description="生态参数编号")
    category_id: int = Field(description="分类编号")


class MappingBatchCreate(BaseModel):
    """一次提交多条对应关系。已经存在的不会报错，会在 existing 里原样返回。"""

    model_config = ConfigDict(title="批量创建生态映射")

    items: list[MappingCreate] = Field(
        default_factory=list, description="要创建的对应关系；可以是空数组"
    )


class MappingResponse(BaseModel):
    """一条生态参数和分类的对应关系。"""

    model_config = ConfigDict(title="生态映射")

    id: int = Field(description="这条对应关系的编号")
    ecological_parameter_id: int = Field(description="生态参数编号")
    category_id: int = Field(description="分类编号")
    created_at: datetime = Field(description="创建时间，UTC 且带时区")


class MappingBatchResponse(BaseModel):
    """批量创建的结果。新的在 created，本来就有的在 existing。"""

    model_config = ConfigDict(title="批量映射结果")

    created: list[MappingResponse] = Field(description="这次新建立的对应关系")
    existing: list[MappingResponse] = Field(description="请求里已经存在、原样返回的对应关系")
    created_count: int = Field(description="新建立的条数")
    existing_count: int = Field(description="已经存在的条数")
