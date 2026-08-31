"""非主键随机标识：trace、租约令牌、瓦片 JWT jti。业务表主键由数据库自增。"""

import uuid

import uuid_utils.compat as _uuid_compat


def new_uuid7() -> uuid.UUID:
    """随机 UUIDv7，只用于令牌和追踪，不作为表主键。"""
    return _uuid_compat.uuid7()
