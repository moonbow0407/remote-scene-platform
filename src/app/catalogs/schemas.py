"""平铺分类 API 模型。"""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255, description="分类名称，全局唯一")

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("name 不能为空")
        return name


class CategoryUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=255, description="新名称")

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("name 不能为空")
        return name


class CategoryResponse(BaseModel):
    id: int = Field(description="分类 ID")
    name: str = Field(description="分类名称")
    created_at: datetime = Field(description="创建时间（UTC，带时区）")
