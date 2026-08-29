"""统一分页基元：所有列表接口返回 items/total/page/page_size。"""

from typing import Annotated

from fastapi import Query
from pydantic import BaseModel

# page_size 上限防止一次性拉取超大列表拖垮 API 与数据库
MAX_PAGE_SIZE = 200


class PageParams:
    """FastAPI 依赖：从查询参数解析并校验分页。"""

    def __init__(
        self,
        page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
        page_size: Annotated[
            int, Query(ge=1, le=MAX_PAGE_SIZE, description=f"每页条数，上限 {MAX_PAGE_SIZE}")
        ] = 20,
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
    """统一分页响应结构。"""

    items: list[T]
    total: int
    page: int
    page_size: int

    @classmethod
    def build(cls, items: list[T], total: int, params: PageParams) -> "Page[T]":
        return cls(items=items, total=total, page=params.page, page_size=params.page_size)
