"""栅格入库流水线：验证 → 哈希去重 → 元数据检查 → COG → 缩略图 → footprint → 完成。

幂等约定：每个步骤先查数据库/对象现状，已完成的工作直接跳过；
每步使用独立数据库事务，部分进度在重试/重投递时保留，不产生重复工件。

资源约定：源文件下载到每任务独立临时目录，绝不整载入内存；
开始前检查临时空间是否满足 源文件 × 倍数 的占用要求。
"""

import logging
import warnings
from pathlib import Path
from typing import Any

import rasterio
from geoalchemy2 import WKTElement
from pyproj import Transformer
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.errors import NotGeoreferencedWarning
from rasterio.shutil import copy as rio_copy
from rasterio.transform import Affine

from app.db import session_scope
from app.imagery.enums import RecordStatus
from app.imagery.service import ImageryService
from app.imagery.types import object_prefix
from app.jobs.enums import JobStatus
from app.jobs.service import JobService
from app.processing.blob import bind_original, ensure_source_local, resolve_input_object
from app.processing.common import (
    IngestionContext,
    cancellation_checkpoint,
    cleanup_tmp_dir,
    is_complete_local_file,
    preflight_tmp,
)
from app.processing.errors import DeterministicError, NeedsInputError, TransientError
from app.processing.render_profile import infer_render_profile
from app.settings import Settings
from app.uploads.minio import MinioAdapter

logger = logging.getLogger(__name__)

_TIFF_MAGICS = (
    b"II*\x00",  # 经典 TIFF，小端序
    b"MM\x00*",  # 经典 TIFF，大端序
    b"II+\x00",  # BigTIFF，小端序
    b"MM\x00+",  # BigTIFF，大端序
)
_THUMBNAIL_MAX_SIDE = 512


def is_supported_tiff_magic(magic: bytes) -> bool:
    """判断文件头是否属于经典 TIFF 或 BigTIFF。"""
    return magic in _TIFF_MAGICS


# 测试与历史 import 路径保持稳定
_is_complete_local_file = is_complete_local_file


def stretch_band_to_uint8(band_data: Any, nodata: float | None) -> Any:
    """把单波段拉伸到 uint8；NaN/Inf 以及 NoData（含 NaN NoData）不参与 min/max。"""
    import numpy as np

    data = np.asarray(band_data, dtype=np.float32)
    valid = np.isfinite(data)
    if nodata is not None and np.isfinite(nodata):
        valid &= data != np.float32(nodata)
    stretched = np.zeros(data.shape, dtype=np.uint8)
    if not np.any(valid):
        return stretched
    finite = data[valid]
    low = float(finite.min())
    high = float(finite.max())
    span = (high - low) or 1.0
    scaled = (data - low) / span * 255.0
    np.clip(scaled, 0, 255, out=scaled)
    stretched[valid] = scaled[valid].astype(np.uint8)
    return stretched


class RasterIngestion:
    """一次任务运行内的流水线执行器；每步独立数据库事务。"""

    def __init__(self, *, settings: Settings, minio: MinioAdapter, engine: Any) -> None:
        self._settings = settings
        self._minio = minio
        self._engine = engine

    def run(self, ctx: IngestionContext) -> None:
        cleanup_tmp_dir(ctx.tmp_dir)
        try:
            preflight_tmp(ctx, self._settings)
            cancellation_checkpoint(ctx, self._engine)
            self._step_validate(ctx)
            cancellation_checkpoint(ctx, self._engine)
            self._step_bind_original(ctx)
            cancellation_checkpoint(ctx, self._engine)
            self._step_inspect(ctx)
            cancellation_checkpoint(ctx, self._engine)
            self._step_create_cog(ctx)
            cancellation_checkpoint(ctx, self._engine)
            self._step_thumbnail(ctx)
            cancellation_checkpoint(ctx, self._engine)
            self._step_footprint(ctx)
            cancellation_checkpoint(ctx, self._engine)
            self._step_finalize(ctx)
        finally:
            cleanup_tmp_dir(ctx.tmp_dir)

    # ---- 步骤实现 ----

    def _step_validate(self, ctx: IngestionContext) -> None:
        """校验输入对象存在、大小一致且为 TIFF；非 TIFF 属确定性错误。

        输入对象按 resolve_input_object 解析：原件键在记录行上。
        """
        key, expected_size = resolve_input_object(engine=self._engine, ctx=ctx)
        stat = self._minio.head_object(key=key)
        if stat is None and key != ctx.source_object_key:
            key = ctx.source_object_key
            expected_size = ctx.source_size_bytes
            stat = self._minio.head_object(key=key)
        if stat is None:
            raise DeterministicError(
                "SOURCE_OBJECT_MISSING",
                f"输入对象不存在：canonical/blob 与上传源 {ctx.source_object_key} 均不可访问",
            )
        if stat["size"] != expected_size:
            raise DeterministicError(
                "SOURCE_SIZE_MISMATCH",
                f"输入对象大小 {stat['size']} 与登记大小 {expected_size} 不一致",
            )
        magic = self._minio.read_head_bytes(key=key, length=4)
        if not is_supported_tiff_magic(magic):
            raise DeterministicError(
                "UNSUPPORTED_FORMAT",
                f"文件魔数 {magic!r} 不是 GeoTIFF；首版仅支持栅格 TIFF 输入",
            )
        self._record_step(ctx, "validate")

    def _step_bind_original(self, ctx: IngestionContext) -> None:
        bind_original(minio=self._minio, engine=self._engine, ctx=ctx)

    def _step_inspect(self, ctx: IngestionContext) -> None:
        """读取栅格元数据并检查地理参考；缺失且未补充时抛 NeedsInputError（不落变更）。

        地理定位与 CRS 是两个独立前提：CRS 只说明坐标含义，GeoTransform 才决定
        影像在哪里。没有可用 GeoTransform 时（rasterio 返回单位阵），即使补充了
        CRS 也必须保持 NEEDS_INPUT，否则像素坐标会被当成真实坐标写入 footprint，
        产生空间位置错误但仍 READY 的数据。
        """
        ensure_source_local(minio=self._minio, engine=self._engine, ctx=ctx)
        with session_scope(self._engine) as session:
            imagery = ImageryService(session)
            record = imagery.get_by_id(ctx.owner_kind, ctx.owner_id)
            assert record is not None
            user_crs = record.user_crs
            with warnings.catch_warnings():
                # 无地理参考时 rasterio 返回单位阵并告警；此处以单位阵作为判定依据，告警无意义
                warnings.simplefilter("ignore", NotGeoreferencedWarning)
                with rasterio.open(ctx.source_path) as dataset:
                    src_crs = dataset.crs
                    transform = dataset.transform
                    if transform == Affine.identity():
                        raise NeedsInputError(
                            reason="MISSING_GEOLOCATION",
                            detail="影像没有可用的 GeoTransform（也没有可用于定位的 GCP）；"
                            "仅有 CRS 无法确定空间位置，不得把像素坐标当作真实坐标。"
                            "请提供带地理参考的影像或定位信息后继续",
                        )
                    if src_crs is None and user_crs is None:
                        raise NeedsInputError(
                            reason="MISSING_CRS",
                            detail="源文件缺少 CRS 且未提供补充信息；"
                            "请提交 EPSG 代码后从断点继续，无需重新上传",
                        )
                    if src_crs is not None:
                        effective_crs = src_crs
                    else:
                        try:
                            effective_crs = CRS.from_user_input(user_crs)
                        except Exception as exc:
                            raise NeedsInputError(
                                reason="INVALID_CRS",
                                detail=f"补充的 CRS {user_crs!r} 无法解析，请提供有效 EPSG 代码",
                            ) from exc
                    profile = infer_render_profile(dataset.count)
                    bands = []
                    for idx in range(1, dataset.count + 1):
                        stats = dataset.statistics(idx, approx=True)
                        bands.append(
                            {
                                "index": idx,
                                "name": dataset.descriptions[idx - 1],
                                "dtype": dataset.dtypes[idx - 1],
                                "min": stats.min,
                                "max": stats.max,
                                "mean": stats.mean,
                            }
                        )
                    imagery.update_fields(
                        ctx.owner_kind,
                        ctx.owner_id,
                        crs=str(effective_crs),
                        width=dataset.width,
                        height=dataset.height,
                        band_count=dataset.count,
                        bands=bands,
                        resolution_x=abs(transform.a),
                        resolution_y=abs(transform.e),
                        nodata=dataset.nodata,
                        render_profile=dict(profile),
                    )
            if record.status is RecordStatus.VALIDATING:
                imagery.set_status(record, RecordStatus.PROCESSING)
        self._record_step(ctx, "inspect")

    def _step_create_cog(self, ctx: IngestionContext) -> None:
        """生成保留源 CRS 的 COG；用户补充 CRS 时经中间层指派（不做重投影）。

        已生成的 COG 不可再以更新模式修改：COG 驱动只支持 CreateCopy，就地更新
        会破坏 COG 优化布局甚至失败。因此源无 CRS 且用户补充了 CRS 时，先把 CRS
        指派到轻量 VRT 中间层（仅元数据、不复制像素），再一次性生成最终 COG。
        """
        with session_scope(self._engine) as session:
            imagery = ImageryService(session)
            record = imagery.get_by_id(ctx.owner_kind, ctx.owner_id)
            assert record is not None
            if (
                record.cog_object_key is not None
                and self._minio.head_object(key=record.cog_object_key) is not None
            ):
                logger.info("COG 已存在，跳过", extra={"owner_id": str(ctx.owner_id)})
                self._record_step(ctx, "create_cog")
                return
            user_crs = record.user_crs

        ensure_source_local(minio=self._minio, engine=self._engine, ctx=ctx)
        cog_tmp = ctx.cog_path
        with rasterio.open(ctx.source_path) as src:
            source_has_crs = src.crs is not None
        if user_crs and not source_has_crs:
            staged = ctx.staged_vrt_path
            rio_copy(str(ctx.source_path), str(staged), driver="VRT")
            with rasterio.open(staged, "r+") as dataset:
                dataset.crs = CRS.from_user_input(user_crs)
            rio_copy(str(staged), str(cog_tmp), driver="COG", compress="DEFLATE", blocksize=512)
        else:
            rio_copy(
                str(ctx.source_path), str(cog_tmp), driver="COG", compress="DEFLATE", blocksize=512
            )
        cog_key = f"{object_prefix(ctx.owner_kind, ctx.owner_id)}/cog.tif"
        content_type = "image/tiff; profile=cloud-optimized"
        self._minio.upload_file(local_path=str(cog_tmp), key=cog_key, content_type=content_type)
        with session_scope(self._engine) as session:
            ImageryService(session).update_fields(
                ctx.owner_kind, ctx.owner_id, cog_object_key=cog_key
            )
        self._record_step(ctx, "create_cog")

    def _step_thumbnail(self, ctx: IngestionContext) -> None:
        """按渲染推断生成 PNG 缩略图（重采样 + 逐波段线性拉伸）。"""
        with session_scope(self._engine) as session:
            imagery = ImageryService(session)
            record = imagery.get_by_id(ctx.owner_kind, ctx.owner_id)
            assert record is not None
            if (
                record.thumbnail_object_key is not None
                and self._minio.head_object(key=record.thumbnail_object_key) is not None
            ):
                self._record_step(ctx, "thumbnail")
                return
            if record.render_profile is None:
                raise TransientError("RENDER_PROFILE_MISSING")
            profile = dict(record.render_profile)
            nodata = record.nodata

        self._ensure_cog_local(ctx)
        bands = [int(b) for b in profile["bands"]]
        mode = profile["mode"]
        with rasterio.open(ctx.cog_path) as src:
            scale = _THUMBNAIL_MAX_SIDE / max(src.width, src.height)
            out_h = max(1, round(src.height * scale))
            out_w = max(1, round(src.width * scale))
            read_idx = [min(b, src.count) for b in bands]
            data = src.read(
                read_idx, out_shape=(len(read_idx), out_h, out_w), resampling=Resampling.bilinear
            )
            out_bands = [stretch_band_to_uint8(band_data, nodata) for band_data in data]
            thumbnail = ctx.thumbnail_path
            if mode == "grayscale" or len(out_bands) == 1:
                with rasterio.open(
                    thumbnail, "w", driver="PNG", height=out_h, width=out_w, count=1, dtype="uint8"
                ) as dst:
                    dst.write(out_bands[0], 1)
            else:
                count = min(3, len(out_bands))
                with rasterio.open(
                    thumbnail,
                    "w",
                    driver="PNG",
                    height=out_h,
                    width=out_w,
                    count=count,
                    dtype="uint8",
                ) as dst:
                    for i in range(count):
                        dst.write(out_bands[i], i + 1)
        thumb_key = f"{object_prefix(ctx.owner_kind, ctx.owner_id)}/thumbnail.png"
        self._minio.upload_file(local_path=str(thumbnail), key=thumb_key, content_type="image/png")
        with session_scope(self._engine) as session:
            ImageryService(session).update_fields(
                ctx.owner_kind, ctx.owner_id, thumbnail_object_key=thumb_key
            )
        self._record_step(ctx, "thumbnail")

    def _step_footprint(self, ctx: IngestionContext) -> None:
        """计算 EPSG:4326 footprint（bbox 多边形）与结构化 bbox 列。"""
        with session_scope(self._engine) as session:
            imagery = ImageryService(session)
            record = imagery.get_by_id(ctx.owner_kind, ctx.owner_id)
            assert record is not None
            if record.footprint is not None or record.crs is None:
                self._record_step(ctx, "footprint")
                return
            crs_text = record.crs
        ensure_source_local(minio=self._minio, engine=self._engine, ctx=ctx)
        with rasterio.open(ctx.source_path) as src:
            bounds = src.bounds
        transformer = Transformer.from_crs(crs_text, "EPSG:4326", always_xy=True)
        min_x, min_y, max_x, max_y = transformer.transform_bounds(
            bounds.left, bounds.bottom, bounds.right, bounds.top, densify_pts=21
        )
        wkt = (
            f"POLYGON(({min_x!r} {min_y!r}, {max_x!r} {min_y!r}, "
            f"{max_x!r} {max_y!r}, {min_x!r} {max_y!r}, {min_x!r} {min_y!r}))"
        )
        with session_scope(self._engine) as session:
            imagery = ImageryService(session)
            imagery.update_fields(
                ctx.owner_kind,
                ctx.owner_id,
                footprint=WKTElement(wkt, srid=4326),
                min_x=min_x,
                min_y=min_y,
                max_x=max_x,
                max_y=max_y,
            )
        self._record_step(ctx, "footprint")

    def _step_finalize(self, ctx: IngestionContext) -> None:
        with session_scope(self._engine) as session:
            imagery = ImageryService(session)
            jobs = JobService(session)
            record = imagery.get_by_id(ctx.owner_kind, ctx.owner_id)
            assert record is not None
            if record.cog_object_key is None or record.thumbnail_object_key is None:
                raise DeterministicError("ARTIFACTS_MISSING", "完成前缺少 COG 或缩略图")
            if record.status is not RecordStatus.READY:
                imagery.set_status(record, RecordStatus.READY)
            job = jobs.get(ctx.job_id)
            if job is not None and job.status is not JobStatus.SUCCEEDED:
                jobs.transition(job, JobStatus.SUCCEEDED, event_type="JOB_SUCCEEDED")

    # ---- 辅助 ----

    def _ensure_cog_local(self, ctx: IngestionContext) -> Path:
        with session_scope(self._engine) as session:
            imagery = ImageryService(session)
            record = imagery.get_by_id(ctx.owner_kind, ctx.owner_id)
            assert record is not None and record.cog_object_key is not None
            key = record.cog_object_key
            expected_size = None
        if expected_size is not None and _is_complete_local_file(ctx.cog_path, expected_size):
            return ctx.cog_path
        ctx.tmp_dir.mkdir(parents=True, exist_ok=True)
        self._minio.download_to_file(key=key, local_path=str(ctx.cog_path))
        return ctx.cog_path

    def _record_step(self, ctx: IngestionContext, step: str) -> None:
        """步骤完成事件（尽力而为，不影响主流程）。"""
        try:
            with session_scope(self._engine) as session:
                JobService(session).append_event(
                    ctx.job_id, event_type="STEP_COMPLETED", detail={"step": step}
                )
        except Exception:
            logger.warning("步骤事件记录失败", extra={"step": step, "job_id": str(ctx.job_id)})
