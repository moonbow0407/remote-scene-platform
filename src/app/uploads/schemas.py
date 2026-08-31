"""上传会话 API 模型。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.assets.enums import AssetSource, AssetType
from app.uploads.models import UploadSessionStatus


class CreateSessionRequest(BaseModel):
    asset_name: str = Field(min_length=1, max_length=255, description="逻辑资产名称")
    asset_type: AssetType = Field(description="物理类型：RASTER 栅格、VECTOR 矢量、ATTACHMENT 附件")
    file_name: str = Field(min_length=1, max_length=512, description="原始文件名，含扩展名")
    size_bytes: int = Field(gt=0, description="文件总字节数")
    part_count: int = Field(
        ge=1, le=10000, description="分片数量，1–10000；大文件必须分片直传 MinIO"
    )
    content_type: str | None = Field(
        default=None, max_length=128, description="MIME 类型，如 image/tiff；可省略"
    )
    source: AssetSource = Field(
        default=AssetSource.UPLOAD,
        description="资产来源：UPLOAD 本机上传、SATELLITE 卫星采集、EXTERNAL_IMPORT 外部导入",
    )
    asset_id: UUID | None = Field(
        default=None, description="为已有资产追加新版本；省略则创建新的逻辑资产"
    )
    resource_catalog_id: UUID | None = Field(default=None, description="资源目录节点 ID，可省略")
    satellite_id: UUID | None = Field(default=None, description="卫星 ID，可省略")
    sensor_id: UUID | None = Field(
        default=None, description="传感器目录 ID，可省略；须属于 satellite_id"
    )
    acquired_at: datetime | None = Field(
        default=None,
        description="数据采集时间，必须带时区；省略时可写在 properties.acquired_at",
    )
    properties: dict[str, Any] = Field(
        default_factory=dict,
        description="扩展业务元数据；采集时间优先用 acquired_at",
    )

    @field_validator("acquired_at")
    @classmethod
    def _acquired_at_tz(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("acquired_at 必须携带时区，例如 2026-08-01T00:00:00+08:00")
        return value


class PartUrl(BaseModel):
    part_number: int = Field(description="分片序号，从 1 开始")
    url: str = Field(description="该分片的 MinIO 预签名 PUT 地址，文件字节直接上传，不经过本 API")


class SessionCreatedResponse(BaseModel):
    session_id: UUID = Field(description="上传会话 ID，后续查询/完成/中止都用它")
    asset_id: UUID = Field(description="逻辑资产 ID")
    upload_id: str = Field(description="MinIO Multipart 上传 ID")
    bucket: str = Field(description="MinIO 桶")
    object_key: str = Field(description="MinIO 对象键")
    part_urls: list[PartUrl] = Field(description="各分片预签名上传地址")
    expires_in_seconds: int = Field(description="预签名 URL 有效期，单位秒")


class UploadedPart(BaseModel):
    part_number: int = Field(description="分片序号，从 1 开始")
    size: int = Field(description="该分片已上传字节数")
    etag: str = Field(description="分片 ETag，完成上传时由 MinIO 校验")


class SessionDetailResponse(BaseModel):
    session_id: UUID = Field(description="上传会话 ID")
    asset_id: UUID = Field(description="逻辑资产 ID")
    status: UploadSessionStatus = Field(
        description="会话状态：PENDING 等待分片、COMPLETED 已完成、ABORTED 已中止"
    )
    file_name: str = Field(description="原始文件名")
    size_bytes: int = Field(description="文件总字节数")
    part_count: int = Field(description="计划分片数")
    uploaded_parts: list[UploadedPart] = Field(description="已经传到 MinIO 的分片")
    missing_part_numbers: list[int] = Field(description="尚未上传的分片序号")

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
    part_number: int = Field(description="分片序号，从 1 开始")
    url: str = Field(description="补签后的预签名 PUT 地址")
    expires_in_seconds: int = Field(description="预签名 URL 有效期，单位秒")


class SessionCompletedResponse(BaseModel):
    session_id: str = Field(description="上传会话 ID")
    asset_id: str = Field(description="逻辑资产 ID")
    asset_version_id: str = Field(description="新创建的资产版本 ID")
    job_id: str | None = Field(description="入库处理任务 ID；附件等可能无需处理则为空")


class SessionAbortResponse(BaseModel):
    session_id: str = Field(description="上传会话 ID")
    status: str = Field(description="中止后的会话状态，固定 ABORTED")
