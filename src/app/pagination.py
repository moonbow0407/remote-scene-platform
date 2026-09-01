"""统一分页基元：所有列表接口返回 items/total/page/page_size。"""

from typing import Annotated

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field

from app.query import blank_as_default

# page_size 上限防止一次性拉取超大列表拖垮 API 与数据库
MAX_PAGE_SIZE = 200
_DEFAULT_PAGE = 1
_DEFAULT_PAGE_SIZE = 20


class PageParams:
    """FastAPI 依赖：从查询参数解析并校验分页。"""

    def __init__(
        self,
        page: Annotated[
            int,
            blank_as_default(_DEFAULT_PAGE),
            Query(ge=1, description="页码，从 1 开始"),
        ] = _DEFAULT_PAGE,
        page_size: Annotated[
            int,
            blank_as_default(_DEFAULT_PAGE_SIZE),
            Query(ge=1, le=MAX_PAGE_SIZE, description=f"每页条数，上限 {MAX_PAGE_SIZE}"),
        ] = _DEFAULT_PAGE_SIZE,
    ) -> None:
        self.page = page
        self.page_size = page_size

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


class Page[T](BaseModel):
    """一页数据。所有列表都是这个结构。"""

    model_config = ConfigDict(title="分页结果")

    items: list[T] = Field(description="这一页的记录")
    total: int = Field(description="符合条件的总条数")
    page: int = Field(description="当前页码，从 1 开始")
    page_size: int = Field(description="每页条数")

    @classmethod
    def build(cls, items: list[T], total: int, params: PageParams) -> "Page[T]":
        return cls(items=items, total=total, page=params.page, page_size=params.page_size)
