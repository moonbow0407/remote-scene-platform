"""SQLAlchemy 会话边界与声明基类。

会话作用域约定：API 以请求为边界，Worker/Dispatcher/Scheduler 以任务/批次为边界，
统一通过 `session_scope` 显式提交或回滚，不在模块层持有长事务。
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.settings import Settings, get_settings

# 约束命名规范：保证 Alembic 自动生成的迁移在升级/降级中可稳定引用约束
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """全部业务模型的声明基类。"""

    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """审计时间戳：统一 UTC（timestamptz），由数据库时钟生成。"""

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


def create_engine(settings: Settings | None = None) -> sa.Engine:
    """创建同步引擎；pool_pre_ping 抵御长连接被基础设施回收。"""
    settings = settings or get_settings()
    return sa.create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
    )


def make_session_factory(engine: sa.Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """显式事务边界：正常返回提交，异常回滚。"""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
