"""Stage 7 可重复静态发布核对：迁移矩阵、OpenAPI、迁移链与遗留依赖。"""

from __future__ import annotations

import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.api.app import API_V1_PREFIX, create_app

ROOT = Path(__file__).resolve().parents[1]
MATRIX_ROW = re.compile(r"^\| (F\d+|U\d+|S\d+|C\d+|R\d+|E\d+|M\d+|T\d+|ST\d+|G\d+) \|")
CLASSIFICATION = re.compile(r"\| (MIGRATED|MERGED|REPLACED|EXCLUDED_WITH_REASON)(?:（| \||$)")


def verify_matrix() -> None:
    rows: dict[str, str] = {}
    for line in (ROOT / "doc/旧接口迁移矩阵.md").read_text(encoding="utf-8").splitlines():
        match = MATRIX_ROW.match(line)
        if match:
            key = match.group(1)
            if key in rows:
                raise RuntimeError(f"迁移矩阵存在重复编号：{key}")
            if CLASSIFICATION.search(line) is None:
                raise RuntimeError(f"迁移矩阵缺少合法分类：{key}")
            rows[key] = line
    if len(rows) != 57:
        raise RuntimeError(f"迁移矩阵应为 55 个 HTTP 端点 + 2 个 Service 能力，实际 {len(rows)}")


def verify_openapi() -> None:
    schema = create_app().openapi()
    operation_ids: list[str] = []
    for path, path_item in schema["paths"].items():
        if not path.startswith(API_V1_PREFIX):
            raise RuntimeError(f"OpenAPI 存在未版本化路径：{path}")
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            operation_ids.append(operation["operationId"])
            media = operation["responses"]["422"]["content"]
            if "application/problem+json" not in media:
                raise RuntimeError(f"{method.upper()} {path} 的 422 未声明 RFC 9457")
    if len(operation_ids) != len(set(operation_ids)):
        raise RuntimeError("OpenAPI operationId 不唯一")
    schemas = schema["components"]["schemas"]
    for name, item in schemas.items():
        if name.startswith("Page_"):
            required = {"items", "total", "page", "page_size"}
            if not required.issubset(item.get("properties", {})):
                raise RuntimeError(f"分页 Schema {name} 不符合统一契约")


def verify_migrations() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    if heads != ["0010"]:
        raise RuntimeError(f"Alembic 应只有 0010 一个 head，实际 {heads}")
    revisions = list(script.walk_revisions(base="base", head="heads"))
    if len(revisions) != 10:
        raise RuntimeError(f"Alembic 迁移链应有 10 个版本，实际 {len(revisions)}")


def verify_no_legacy_runtime_dependency() -> None:
    forbidden = ("AjaxResult", "TableDataInfo", "C:/ruoyi/uploadPath", "mysql+")
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                raise RuntimeError(f"运行时代码仍包含遗留依赖 {token!r}：{path}")


def main() -> None:
    verify_matrix()
    verify_openapi()
    verify_migrations()
    verify_no_legacy_runtime_dependency()
    print("Stage 7 静态发布核对通过：57 项矩阵、OpenAPI、10 段迁移链、遗留依赖。")


if __name__ == "__main__":
    main()
