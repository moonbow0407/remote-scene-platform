"""核心主键生成：统一 UUIDv7，数据库类型为 PostgreSQL `uuid`。

UUIDv7 自带时间序，便于 B-tree 局部性与按主键粗排；生成在应用侧完成，
不依赖数据库扩展。
"""

import uuid

import uuid_utils.compat as _uuid_compat


def new_uuid7() -> uuid.UUID:
    """生成一个 UUIDv7（返回标准库 uuid.UUID 类型）。"""
    return _uuid_compat.uuid7()
