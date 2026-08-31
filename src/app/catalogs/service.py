"""平铺分类服务。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.catalogs.models import Category
from app.catalogs.schemas import CategoryCreate, CategoryUpdate
from app.context import get_actor
from app.errors import conflict, not_found
from app.pagination import Page, PageParams


def _actor_id() -> int | None:
    actor = get_actor()
    if actor.actor_id is None:
        return None
    try:
        return int(actor.actor_id)
    except ValueError:
        return None


class CatalogService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def list_categories(self, pagination: PageParams, *, q: str | None = None) -> Page[Category]:
        stmt = sa.select(Category)
        count_stmt = sa.select(sa.func.count()).select_from(Category)
        if q:
            pattern = f"%{q.strip()}%"
            stmt = stmt.where(Category.name.ilike(pattern))
            count_stmt = count_stmt.where(Category.name.ilike(pattern))
        total = int(self._session.scalar(count_stmt) or 0)
        rows = list(
            self._session.scalars(
                stmt.order_by(Category.name).offset(pagination.offset).limit(pagination.limit)
            )
        )
        return Page[Category](
            items=rows, total=total, page=pagination.page, page_size=pagination.page_size
        )

    def get_required(self, category_id: int) -> Category:
        row = self._session.get(Category, category_id)
        if row is None:
            raise not_found("分类", category_id)
        return row

    def create(self, body: CategoryCreate) -> Category:
        row = Category(name=body.name, created_by=_actor_id())
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise conflict(
                code="CATEGORY_NAME_TAKEN", detail=f"分类名称已存在：{body.name}"
            ) from exc
        return row

    def update(self, category_id: int, body: CategoryUpdate) -> Category:
        row = self.get_required(category_id)
        row.name = body.name
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise conflict(
                code="CATEGORY_NAME_TAKEN", detail=f"分类名称已存在：{body.name}"
            ) from exc
        return row

    def delete(self, category_id: int) -> None:
        row = self.get_required(category_id)
        from app.assets.models import DataAsset

        in_use = self._session.scalar(
            sa.select(sa.func.count()).where(DataAsset.category_id == category_id)
        )
        if int(in_use or 0) > 0:
            raise conflict(
                code="CATEGORY_IN_USE",
                detail=f"分类 {category_id} 仍被资产引用，不能删除",
            )
        try:
            self._session.delete(row)
            self._session.flush()
        except IntegrityError as exc:
            raise conflict(
                code="CATEGORY_IN_USE",
                detail=f"分类 {category_id} 仍被其他记录引用，不能删除",
            ) from exc

    def resolve_category_id(self, category_id: int | None) -> int | None:
        if category_id is None:
            return None
        self.get_required(category_id)
        return category_id
