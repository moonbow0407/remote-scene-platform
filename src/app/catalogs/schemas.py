"""分类接口的请求和响应。分类是平铺列表，没有上下级。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CategoryCreate(BaseModel):
    """新建一个分类，名称全局不能重复。"""

    model_config = ConfigDict(title="创建分类")

    name: str = Field(
        min_length=1,
        max_length=255,
        description="分类名称，例如「卫星影像」。全局唯一",
        examples=["卫星影像"],
    )

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("name 不能为空")
        return name


class CategoryUpdate(BaseModel):
    """只改名称。"""

    model_config = ConfigDict(title="重命名分类")

    name: str = Field(min_length=1, max_length=255, description="新名称，全局唯一")

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("name 不能为空")
        return name


class CategoryResponse(BaseModel):
    """一个分类。"""

    model_config = ConfigDict(title="分类")

    id: int = Field(description="分类编号")
    name: str = Field(description="分类名称")
    created_at: datetime = Field(description="创建时间，UTC 且带时区")
