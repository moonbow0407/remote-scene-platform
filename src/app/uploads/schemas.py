"""上传会话 API 模型。"""

from typing import Any

from pydantic import BaseModel, Field

from app.assets.enums import AssetType
from app.uploads.models import UploadSessionStatus


class CreateSessionRequest(BaseModel):
    file_name: str = Field(min_length=1, max_length=512, description="原始文件名，含扩展名")
    size_bytes: int = Field(gt=0, description="文件总字节数")
    content_type: str | None = Field(default=None, max_length=128, description="MIME，可省略")
    asset_type: AssetType | None = Field(
        default=None,
        description="省略时按扩展名判断：tif 栅格，zip/geojson/gpkg 矢量，其余附件",
    )


class PartUrl(BaseModel):
    part_number: int = Field(description="分片序号，从 1 开始")
    url: str = Field(description="该分片的 MinIO 预签名 PUT 地址")


class SessionCreatedResponse(BaseModel):
    session_id: int = Field(description="上传会话 ID")
    asset_id: int = Field(description="资产 ID，上传完成后轮询 GET /assets/{id}")
    part_urls: list[PartUrl] = Field(description="各分片预签名上传地址")
    expires_in_seconds: int = Field(description="预签名 URL 有效期，单位秒")


class UploadedPart(BaseModel):
    part_number: int
    size: int
    etag: str


class SessionDetailResponse(BaseModel):
    session_id: int
    asset_id: int
    status: UploadSessionStatus
    file_name: str
    size_bytes: int
    part_count: int
    uploaded_parts: list[UploadedPart]
    missing_part_numbers: list[int]

    @classmethod
    def build(cls, session: Any, uploaded_parts: list[UploadedPart]) -> "SessionDetailResponse":
        uploaded_numbers = {p.part_number for p in uploaded_parts}
        return cls(
            session_id=session.id,
            asset_id=session.asset_id,
            status=session.status,
            file_name=session.file_name,
            size_bytes=session.size_bytes,
            part_count=session.part_count,
            uploaded_parts=uploaded_parts,
            missing_part_numbers=[
                n for n in range(1, session.part_count + 1) if n not in uploaded_numbers
            ],
        )


class PartUrlResponse(BaseModel):
    part_number: int
    url: str
    expires_in_seconds: int


class SessionCompletedResponse(BaseModel):
    session_id: int
    asset_id: int


class SessionAbortResponse(BaseModel):
    session_id: int
    status: str
