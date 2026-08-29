"""Alembic 环境：数据库 URL 统一来自应用配置，支持离线与在线模式。"""

from sqlalchemy import create_engine, pool, text

from alembic import context
from app.assets import models as _assets_models  # noqa: F401
from app.auth import models as _auth_models  # noqa: F401
from app.db import Base
from app.jobs import models as _jobs_models  # noqa: F401
from app.settings import get_settings
from app.uploads import models as _uploads_models  # noqa: F401
from app.vector_features import models as _vector_models  # noqa: F401

# 导入全部模型模块，使 Base.metadata 包含完整表定义（自动生成迁移依赖）
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(get_settings().database_url, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        # PostGIS 镜像可能从 template1 预置 postgis_tiger_geocoder/topology。
        # 这些表由 extension 管理，Alembic 不得生成 DROP；普通业务多余表仍参与漂移检查。
        extension_relations = set(
            connection.execute(
                text(
                    """
                    SELECT c.relname
                    FROM pg_depend AS d
                    JOIN pg_extension AS e ON e.oid = d.refobjid
                    JOIN pg_class AS c ON c.oid = d.objid
                    WHERE d.deptype = 'e'
                    """
                )
            ).scalars()
        )
        # 上述只读查询会触发 SQLAlchemy autobegin；必须先结束它，随后由 Alembic
        # 的 begin_transaction 独立提交版本表和 DDL，避免连接关闭时整体回滚。
        connection.commit()

        def include_object(
            object_: object,
            name: str | None,
            type_: str,
            reflected: bool,
            compare_to: object | None,
        ) -> bool:
            return not (
                reflected
                and compare_to is None
                and type_ == "table"
                and name in extension_relations
            )

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
