"""上传接口的请求和响应。"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.assets.enums import AssetType
from app.uploads.models import UploadSessionStatus


class CreateSessionRequest(BaseModel):
    """创建一次上传。只需文件名和大小；服务端按大小切分，并返回每片的临时 PUT 地址。"""

    model_config = ConfigDict(title="创建上传")

    file_name: str = Field(
        min_length=1,
        max_length=512,
        description="原始文件名，必须带扩展名。用扩展名判断文件种类",
        examples=["GF1_PMS_20240101.tif"],
    )
    size_bytes: int = Field(gt=0, description="文件总大小，单位字节", examples=[104857600])
    content_type: str | None = Field(
        default=None, max_length=128, description="文件 MIME 类型，可不传"
    )
    asset_type: AssetType | None = Field(
        default=None,
        description="文件种类。不传则按扩展名判断：tif 栅格，zip/geojson/gpkg/shp 矢量，其余附件",
    )


class PartUrl(BaseModel):
    """一片文件对应的临时上传地址。"""

    model_config = ConfigDict(title="分片上传地址")

    part_number: int = Field(description="分片序号，从 1 开始")
    url: str = Field(description="该片的临时 PUT 地址。把这一片的字节直接 PUT 上去，不要经过本服务")


class SessionCreatedResponse(BaseModel):
    """上传会话已创建。按 part_urls 传完所有分片后，调用「完成上传」。"""

    model_config = ConfigDict(title="上传已创建")

    session_id: int = Field(description="本次上传编号")
    asset_id: int = Field(description="对应的资产编号。传完后用它请求「资产详情」看处理进度")
    part_urls: list[PartUrl] = Field(description="每一片的临时 PUT 地址")
    expires_in_seconds: int = Field(description="这些上传地址的有效时间，单位秒")


class UploadedPart(BaseModel):
    """对象存储里已经收到的一片。"""

    model_config = ConfigDict(title="已上传分片")

    part_number: int = Field(description="分片序号，从 1 开始")
    size: int = Field(description="这一片的字节数")
    etag: str = Field(description="存储返回的分片校验值，排查缺片时用")


class SessionDetailResponse(BaseModel):
    """上传进行到哪一步、哪些片还没传。中断后续传时看 missing_part_numbers。"""

    model_config = ConfigDict(title="上传详情")

    session_id: int = Field(description="本次上传编号")
    asset_id: int = Field(description="对应的资产编号")
    status: UploadSessionStatus = Field(description="上传状态")
    file_name: str = Field(description="原始文件名")
    size_bytes: int = Field(description="文件总大小，单位字节")
    part_count: int = Field(description="应传的分片总数")
    uploaded_parts: list[UploadedPart] = Field(description="已经传到存储的分片")
    missing_part_numbers: list[int] = Field(description="还没传到的分片序号")

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
    """重新签发某一片的临时 PUT 地址。"""

    model_config = ConfigDict(title="补签分片地址")

    part_number: int = Field(description="分片序号，从 1 开始")
    url: str = Field(description="该片的临时 PUT 地址")
    expires_in_seconds: int = Field(description="该地址有效时间，单位秒")


class SessionCompletedResponse(BaseModel):
    """分片已合并，后台开始处理。接下来轮询 GET /assets/{asset_id}。"""

    model_config = ConfigDict(title="上传完成")

    session_id: int = Field(description="本次上传编号")
    asset_id: int = Field(description="资产编号，用它查询处理进度")


class SessionAbortResponse(BaseModel):
    """这次上传已取消，对应资产会记为失败。"""

    model_config = ConfigDict(title="上传已中止")

    session_id: int = Field(description="本次上传编号")
    status: str = Field(description="中止后的状态，一般为 ABORTED")
