"""矢量/附件/栅格输入的文件头与归档探测（纯标准库，API 与 Worker 均可导入）。"""

from __future__ import annotations

import zipfile
from enum import StrEnum
from pathlib import Path

from app.processing.errors import DeterministicError
from app.processing.geojson_stream import peek_geojson_root_type

_TIFF_MAGICS = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
_SQLITE_MAGIC = b"SQLite format 3"
_SHAPEFILE_REQUIRED = (".shp", ".shx", ".dbf")


class DetectedKind(StrEnum):
    TIFF = "TIFF"
    GEOJSON = "GEOJSON"
    SHAPEFILE_ZIP = "SHAPEFILE_ZIP"
    GEOPACKAGE = "GEOPACKAGE"
    PDF = "PDF"
    UNKNOWN = "UNKNOWN"


def sniff_head(magic: bytes) -> DetectedKind:
    """只根据文件头做廉价判断；ZIP 是否为合法 Shapefile 需打开完整对象。"""
    if magic[:4] in _TIFF_MAGICS:
        return DetectedKind.TIFF
    if magic.startswith(b"%PDF"):
        return DetectedKind.PDF
    if magic.startswith(_SQLITE_MAGIC):
        return DetectedKind.GEOPACKAGE
    if magic[:2] == b"PK":
        return DetectedKind.SHAPEFILE_ZIP
    stripped = magic.lstrip(b"\xef\xbb\xbf \t\r\n")
    if stripped[:1] in (b"{", b"["):
        return DetectedKind.GEOJSON
    return DetectedKind.UNKNOWN


def detect_file(path: Path) -> DetectedKind:
    """下载到本地后的完整探测；损坏 ZIP 视为确定性错误。

    只读文件头 64 字节再按类型做廉价校验，禁止 path.read_bytes() 把整个对象载入内存。
    """
    with path.open("rb") as handle:
        head = handle.read(64)
    kind = sniff_head(head)
    if kind is DetectedKind.SHAPEFILE_ZIP:
        _require_shapefile_zip(path)
        return DetectedKind.SHAPEFILE_ZIP
    if kind is DetectedKind.GEOJSON:
        _require_geojson(path)
        return DetectedKind.GEOJSON
    if kind is DetectedKind.GEOPACKAGE:
        _require_geopackage(path)
        return DetectedKind.GEOPACKAGE
    return kind


def _require_shapefile_zip(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            names = [
                _safe_zip_name(info.filename)
                for info in archive.infolist()
                if not info.is_dir()
            ]
    except zipfile.BadZipFile as exc:
        raise DeterministicError(
            "INVALID_VECTOR_ARCHIVE", "不是合法 ZIP，无法作为 Shapefile 压缩包"
        ) from exc
    stems: dict[str, set[str]] = {}
    for name in names:
        suffix = Path(name).suffix.lower()
        stem = Path(name).stem.lower()
        stems.setdefault(stem, set()).add(suffix)
    complete = [
        stem
        for stem, suffixes in stems.items()
        if all(ext in suffixes for ext in _SHAPEFILE_REQUIRED)
    ]
    if not complete:
        raise DeterministicError(
            "INVALID_VECTOR_ARCHIVE",
            "ZIP 中缺少成套 .shp/.shx/.dbf，不能作为 Shapefile 导入",
        )


def _safe_zip_name(raw: str) -> str:
    name = raw.replace("\\", "/")
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        raise DeterministicError("INVALID_VECTOR_ARCHIVE", f"ZIP 含非法路径：{raw}")
    return name


def _require_geojson(path: Path) -> None:
    root_type = peek_geojson_root_type(path)
    if root_type not in ("FeatureCollection", "Feature"):
        raise DeterministicError(
            "INVALID_VECTOR_ARCHIVE", "GeoJSON 必须是 FeatureCollection 或 Feature"
        )


def _require_geopackage(path: Path) -> None:
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='gpkg_contents'"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise DeterministicError("INVALID_VECTOR_ARCHIVE", "不是合法 GeoPackage（SQLite）") from exc
    if not rows:
        raise DeterministicError(
            "INVALID_VECTOR_ARCHIVE", "SQLite 文件缺少 gpkg_contents，不是 GeoPackage"
        )
