"""Stage 2 ORM 关系必须能在无数据库连接时完成配置。"""

from importlib import import_module

from sqlalchemy.orm import configure_mappers


def test_all_mappers_configure_without_ambiguous_foreign_keys() -> None:
    for module in (
        "app.assets.models",
        "app.auth.models",
        "app.jobs.models",
        "app.uploads.models",
        "app.catalogs.models",
        "app.ecology.models",
        "app.vector_features.models",
    ):
        import_module(module)
    configure_mappers()
