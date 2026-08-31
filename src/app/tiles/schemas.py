"""瓦片签发 API 模型。"""

from pydantic import BaseModel, Field


class TileUrlResponse(BaseModel):
    asset_id: int = Field(description="资产 ID，瓦片绑定到该资产的 COG")
    tile_url_template: str = Field(
        description=(
            "XYZ 瓦片 URL 模板，含 {z}/{x}/{y} 与短期 token；必须经本平台网关，不要直连 TiTiler"
        ),
    )
    tile_json_url: str = Field(description="TileJSON 地址，同样带短期 token")
    token_expires_at: int = Field(description="令牌过期时间，Unix 秒")
    ttl_seconds: int = Field(description="令牌有效期，单位秒；过期后需重新申请")
