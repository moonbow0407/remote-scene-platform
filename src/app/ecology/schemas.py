"""生态参数及其与分类的对应关系。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.ecology.enums import EcologicalParameterStatus
from app.ecology.majors import ABBREV_PATTERN, ITEM_CODE_PATTERN


def _validate_item_code(value: str) -> str:
    code = value.strip()
    if not ITEM_CODE_PATTERN.fullmatch(code):
        raise ValueError("code 须为 4 位数字细项编号，例如 0102")
    return code


def _validate_abbrev(value: str) -> str:
    abbrev = value.strip()
    if not ABBREV_PATTERN.fullmatch(abbrev):
        raise ValueError("abbrev 须为 1–64 字符，以字母或数字开头，可含点、下划线与连字符")
    return abbrev


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


class EcologicalParameterCreate(BaseModel):
    """新建一条生态参量细项。大类由细项编号前两位决定。"""

    model_config = ConfigDict(title="创建生态参数")

    code: str = Field(
        min_length=4,
        max_length=4,
        description="细项编号，四位数字，全局唯一，例如 0102",
        examples=["0102"],
    )
    name: str = Field(min_length=1, max_length=255, description="中文名称")
    abbrev: str = Field(
        min_length=1,
        max_length=64,
        description="英文缩写，全局唯一，例如 NDVI",
        examples=["NDVI"],
    )
    english_name: str | None = Field(default=None, max_length=255, description="英文全称；不传为空")
    major_name: str | None = Field(
        default=None,
        max_length=255,
        description="大类名称。01–07 可省略；其它大类必须传",
    )
    status: EcologicalParameterStatus = Field(
        default=EcologicalParameterStatus.ACTIVE, description="是否启用"
    )
    sort_order: int = Field(
        default=0, ge=0, le=1_000_000, description="同一大类里的排序，数字越小越靠前"
    )
    remark: str | None = Field(default=None, max_length=2000, description="备注；不传为空")

    @field_validator("code")
    @classmethod
    def _code(cls, value: str) -> str:
        return _validate_item_code(value)

    @field_validator("abbrev")
    @classmethod
    def _abbrev(cls, value: str) -> str:
        return _validate_abbrev(value)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("name 不能为空")
        return name

    @field_validator("english_name", "major_name", "remark")
    @classmethod
    def _optional(cls, value: str | None) -> str | None:
        return _optional_text(value)


class EcologicalParameterUpdate(BaseModel):
    """改生态参数。没写的字段保持原值。改 code 会同步重算大类。"""

    model_config = ConfigDict(title="更新生态参数")

    code: str | None = Field(
        default=None, min_length=4, max_length=4, description="新细项编号；不传则不改"
    )
    name: str | None = Field(
        default=None, min_length=1, max_length=255, description="新中文名称；不传则不改"
    )
    abbrev: str | None = Field(
        default=None, min_length=1, max_length=64, description="新英文缩写；不传则不改"
    )
    english_name: str | None = Field(
        default=None, max_length=255, description="英文全称；不传则不改，传空串视为清空"
    )
    major_name: str | None = Field(
        default=None, max_length=255, description="大类名称；已知大类不允许改成别的名字"
    )
    status: EcologicalParameterStatus | None = Field(
        default=None, description="启用状态；不传则不改"
    )
    sort_order: int | None = Field(
        default=None, ge=0, le=1_000_000, description="同层排序；不传则不改"
    )
    remark: str | None = Field(
        default=None, max_length=2000, description="备注；不传则不改，传空串视为清空"
    )

    @field_validator("code")
    @classmethod
    def _code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_item_code(value)

    @field_validator("abbrev")
    @classmethod
    def _abbrev(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_abbrev(value)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        name = value.strip()
        if not name:
            raise ValueError("name 不能为空")
        return name

    @field_validator("english_name", "major_name", "remark")
    @classmethod
    def _optional(cls, value: str | None) -> str | None:
        return _optional_text(value)


class EcologicalParameterResponse(BaseModel):
    """一条生态参量细项。"""

    model_config = ConfigDict(title="生态参数")

    id: int = Field(description="生态参数编号，检索和映射用这个")
    code: str = Field(description="细项编号，四位数字，例如 0102")
    name: str = Field(description="中文名称")
    abbrev: str = Field(description="英文缩写，例如 NDVI")
    english_name: str | None = Field(description="英文全称；没有则为空")
    major_code: str = Field(description="大类编号，例如 01")
    major_name: str = Field(description="大类名称，例如 生物参数")
    remark: str | None = Field(description="备注；没有则为空")
    status: EcologicalParameterStatus = Field(description="是否启用")
    sort_order: int = Field(description="同一大类里的排序")
    created_at: datetime = Field(description="创建时间，UTC 且带时区")
    updated_at: datetime = Field(description="最近一次修改时间，UTC 且带时区")


class EcologicalParameterLeaf(BaseModel):
    """生态参数树的叶子。只有叶子有 id，才能用于检索。"""

    model_config = ConfigDict(title="生态参数细项")

    id: int = Field(description="生态参数编号")
    code: str = Field(description="细项编号")
    abbrev: str = Field(description="英文缩写")
    name: str = Field(description="中文名称")
    english_name: str | None = Field(description="英文全称；没有则为空")
    status: EcologicalParameterStatus = Field(description="是否启用")
    sort_order: int = Field(description="同一大类里的排序")
    remark: str | None = Field(description="备注；没有则为空")


class EcologicalParameterMajorNode(BaseModel):
    """按大类分组的树节点。根没有参数 id。"""

    model_config = ConfigDict(title="生态参数大类")

    major_code: str = Field(description="大类编号")
    major_name: str = Field(description="大类名称")
    children: list[EcologicalParameterLeaf] = Field(description="该大类下的细项")


class MajorResponse(BaseModel):
    """一个生态参量大类。"""

    model_config = ConfigDict(title="生态参量大类")

    code: str = Field(description="大类编号，例如 01")
    name: str = Field(description="大类名称，例如 生物参数")


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
