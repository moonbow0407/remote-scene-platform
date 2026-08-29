"""上传会话 API 模型。"""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.assets.enums import AssetSource, AssetType
from app.uploads.models import UploadSessionStatus


class CreateSessionRequest(BaseModel):
    asset_name: str = Field(min_length=1, max_length=255, description="逻辑资产名称")
    asset_type: AssetType = Field(description="物理类型：栅格/矢量/附件")
    file_name: str = Field(min_length=1, max_length=512, description="原始文件名")
    size_bytes: int = Field(gt=0, description="文件总字节数")
    part_count: int = Field(ge=1, le=10000, description="分片数量（1..10000）")
    content_type: str | None = Field(default=None, max_length=128)
    source: AssetSource = Field(default=AssetSource.UPLOAD, description="资产来源")
    asset_id: UUID | None = Field(
        default=None, description="为已有资产追加新版本；空则创建新逻辑资产"
    )
    resource_catalog_id: UUID | None = Field(default=None, description="业务分类：资源目录主键")
    satellite_id: UUID | None = Field(default=None, description="平台：卫星主键")
    sensor_id: UUID | None = Field(default=None, description="仪器：传感器主键")
    properties: dict[str, Any] = Field(
        default_factory=dict, description="业务元数据，可含 ISO8601 acquired_at"
    )


class PartUrl(BaseModel):
    part_number: int
    url: str


class SessionCreatedResponse(BaseModel):
    session_id: UUID
    asset_id: UUID
    upload_id: str
    object_key: str
    part_urls: list[PartUrl]
    expires_in_seconds: int


class UploadedPart(BaseModel):
    part_number: int
    size: int
    etag: str


class SessionDetailResponse(BaseModel):
    session_id: UUID
    asset_id: UUID
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
    session_id: str
    asset_id: str
    asset_version_id: str
    job_id: str | None
