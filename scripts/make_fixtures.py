"""生成 Stage 0 定义的测试夹具（合成数据，小体积，含真实 CRS/波段/几何元数据）。

用法：uv run python scripts/make_fixtures.py
X6 GeoPackage 由 worker 镜像内的 ogr2ogr 生成（见 README/验收基线），不在此脚本内。
"""

import json
import random
import zipfile
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

random.seed(42)
np.random.seed(42)


def make_multiband() -> None:
    """X1：4 波段 Byte，512×512，EPSG:32650，带纹理。"""
    path = FIXTURES / "multiband.tif"
    height, width = 512, 512
    transform = from_origin(500000, 4200000, 10, 10)
    data = np.zeros((4, height, width), dtype="uint8")
    yy, xx = np.mgrid[0:height, 0:width]
    for band in range(3):
        data[band] = (
            (xx * (band + 1) + yy * (3 - band)) % 256
            + np.random.randint(0, 24, size=(height, width))
        ).astype("uint8")
    data[3] = 7  # 近常值波段，用于渲染/统计断言
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=4,
        dtype="uint8",
        crs="EPSG:32650",
        transform=transform,
        compress="deflate",
    ) as dst:
        dst.write(data)
        dst.descriptions = ("red", "green", "blue", "nir")
    print(f"written {path}")


def make_singleband() -> None:
    """X2：单波段 UInt16，256×256，EPSG:4326。"""
    path = FIXTURES / "singleband.tif"
    height, width = 256, 256
    transform = from_origin(114.0, 31.0, 0.004, 0.004)
    yy, xx = np.mgrid[0:height, 0:width]
    data = ((xx + yy) * 200 % 60000).astype("uint16")
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="uint16",
        crs="EPSG:4326",
        transform=transform,
    ) as dst:
        dst.write(data, 1)
        dst.descriptions = ("elevation",)
    print(f"written {path}")


def make_no_crs() -> None:
    """X3：3 波段 Byte，无 CRS、无地理参考（NEEDS_INPUT 夹具）。"""
    path = FIXTURES / "no_crs.tif"
    height, width = 128, 128
    data = np.random.randint(0, 255, size=(3, height, width)).astype("uint8")
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=3,
        dtype="uint8",
        # 故意不写 crs 与 transform
    ) as dst:
        dst.write(data)
    print(f"written {path}")


def make_points_geojson() -> None:
    """X4：EPSG:4326 的 50 个 Point，属性含字符串/数值/null。"""
    path = FIXTURES / "points.geojson"
    features = []
    for i in range(50):
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [
                        round(114.0 + random.random() * 1.0, 6),
                        round(30.0 + random.random() * 1.0, 6),
                    ],
                },
                "properties": {
                    "name": f"point-{i:03d}",
                    "value": i * 3,
                    "note": None if i % 5 == 0 else f"note-{i}",
                },
            }
        )
    fc = {"type": "FeatureCollection", "features": features}
    path.write_text(json.dumps(fc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"written {path}")


def make_polygon_shapefile_zip() -> None:
    """X5：EPSG:4326 的 20 个 Polygon，打包 .shp/.shx/.dbf/.prj。

    使用 pyshp（纯 Python）生成；运行环境需安装 pyshp（dev 依赖组）。
    """
    import shapefile  # pyshp

    base = FIXTURES / "polygons_shp"
    w = shapefile.Writer(str(base), shapeType=shapefile.POLYGON)
    w.field("name", "C", size=40)
    w.field("value", "N", size=10, decimal=0)
    for i in range(20):
        x0 = 114.0 + (i % 5) * 0.1
        y0 = 30.0 + (i // 5) * 0.1
        ring = [
            [x0, y0],
            [x0 + 0.08, y0],
            [x0 + 0.08, y0 + 0.08],
            [x0, y0 + 0.08],
            [x0, y0],
        ]
        w.poly([ring])
        w.record(f"poly-{i:02d}", i)
    with open(FIXTURES / "polygons_shp.prj", "w", encoding="ascii") as prj:
        prj.write(
            'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
            'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]'
        )
    w.close()
    zip_path = FIXTURES / "polygons_shp.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for ext in ("shp", "shx", "dbf", "prj"):
            zf.write(FIXTURES / f"polygons_shp.{ext}", arcname=f"polygons_shp.{ext}")
    for ext in ("shp", "shx", "dbf", "prj"):
        (FIXTURES / f"polygons_shp.{ext}").unlink()
    print(f"written {zip_path}")


def make_lines_geojson_3857() -> None:
    """X6 源：EPSG:3857 的 30 个 LineString；GeoPackage 由 ogr2ogr 转换生成。"""
    path = FIXTURES / "lines_3857.geojson"
    features = []
    for i in range(30):
        x0 = 12690000 + (i % 6) * 10000
        y0 = 3500000 + (i // 6) * 10000
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [x0, y0],
                        [x0 + 8000, y0 + 4000],
                        [x0 + 16000, y0],
                    ],
                },
                "properties": {"name": f"line-{i:02d}", "length_m": 24000, "kind": None},
            }
        )
    fc = {"type": "FeatureCollection", "features": features}
    path.write_text(json.dumps(fc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"written {path}")
    print("提示：在 worker 容器内执行 ogr2ogr 生成 lines.gpkg（EPSG:3857）：")
    print(
        "docker compose run --rm --no-deps -v ./:/work worker sh -c "
        "'ogr2ogr -f GPKG /work/tests/fixtures/lines.gpkg "
        "/work/tests/fixtures/lines_3857.geojson -a_srs EPSG:3857'"
    )


def make_attachment() -> None:
    """X7：数百 KB 的合法最小 PDF。"""
    path = FIXTURES / "report.pdf"
    content = b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    content += b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    content += b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    content += b"xref\n0 4\ntrailer<</Size 4/Root 1 0 R>>\n%%EOF\n"
    content += b"%% padding to reach ~200KB: " + b"P" * (200 * 1024 - len(content))
    path.write_bytes(content)
    print(f"written {path}")


if __name__ == "__main__":
    FIXTURES.mkdir(parents=True, exist_ok=True)
    make_multiband()
    make_singleband()
    make_no_crs()
    make_points_geojson()
    make_polygon_shapefile_zip()
    make_lines_geojson_3857()
    make_attachment()
