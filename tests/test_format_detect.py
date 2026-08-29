"""矢量/附件文件头与归档探测。"""

import json
import zipfile
from pathlib import Path

import pytest

from app.processing.detect import DetectedKind, detect_file, sniff_head
from app.processing.errors import DeterministicError


def test_sniff_tiff_pdf_sqlite_and_json() -> None:
    assert sniff_head(b"II*\x00xxxx") is DetectedKind.TIFF
    assert sniff_head(b"%PDF-1.4") is DetectedKind.PDF
    assert sniff_head(b"SQLite format 3\x00") is DetectedKind.GEOPACKAGE
    assert sniff_head(b'{"type":"FeatureCollection"}') is DetectedKind.GEOJSON
    assert sniff_head(b"PK\x03\x04") is DetectedKind.SHAPEFILE_ZIP
    assert sniff_head(b"XXXX") is DetectedKind.UNKNOWN


def test_detect_geojson_fixture() -> None:
    path = Path("tests/fixtures/points.geojson")
    assert detect_file(path) is DetectedKind.GEOJSON


def test_detect_geopackage_fixture() -> None:
    path = Path("tests/fixtures/lines.gpkg")
    if not path.is_file():
        pytest.skip("尚未生成 tests/fixtures/lines.gpkg")
    assert detect_file(path) is DetectedKind.GEOPACKAGE


def test_detect_shapefile_zip_fixture() -> None:
    path = Path("tests/fixtures/polygons_shp.zip")
    assert detect_file(path) is DetectedKind.SHAPEFILE_ZIP


def test_invalid_zip_is_deterministic(tmp_path: Path) -> None:
    bogus = tmp_path / "fake.zip"
    bogus.write_bytes(b"PK\x03\x04not-a-zip")
    with pytest.raises(DeterministicError) as exc:
        detect_file(bogus)
    assert exc.value.code == "INVALID_VECTOR_ARCHIVE"


def test_zip_without_shapefile_parts_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "empty.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("readme.txt", "no shapefile")
    with pytest.raises(DeterministicError) as exc:
        detect_file(archive)
    assert "shp" in exc.value.detail


def test_geojson_must_be_feature_collection(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"type": "Point", "coordinates": [0, 0]}), encoding="utf-8")
    with pytest.raises(DeterministicError):
        detect_file(path)
