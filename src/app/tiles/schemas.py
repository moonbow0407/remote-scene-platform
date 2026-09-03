"""地图地址。"""

from pydantic import BaseModel, ConfigDict, Field

from app.imagery.enums import RecordKind


class TileUrlResponse(BaseModel):
    """栅格在地图上显示用的短期地址。过期后重新申请，不要存成永久链接。"""

    model_config = ConfigDict(title="地图地址")

    kind: RecordKind = Field(description="SATELLITE 或 UAV")
    id: int = Field(description="记录编号")
    tile_url_template: str = Field(
        description="XYZ 瓦片地址模板，含 {z}/{x}/{y} 和短期令牌。必须走本平台网关，不要改主机名"
    )
    tile_json_url: str = Field(description="TileJSON 地址，同样带短期令牌")
    token_expires_at: int = Field(description="令牌过期时刻，Unix 秒")
    ttl_seconds: int = Field(description="令牌有效时间，单位秒；过期后重新申请")
