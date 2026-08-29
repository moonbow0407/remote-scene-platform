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
    """X3：3 波段 Byte，无 CRS、有 EPSG:4326 度单位 GeoTransform（NEEDS_INPUT 夹具）。

    保留有效 GeoTransform 使"补充 CRS → 断点恢复"链路可测；无 GeoTransform 的数据
    无法凭 CRS 定位（像素坐标不等于真实坐标），按 MISSING_GEOLOCATION 阻塞，
    不作为缺 CRS 夹具使用。
    """
    path = FIXTURES / "no_crs.tif"
    height, width = 128, 128
    transform = from_origin(114.0, 31.0, 0.004, 0.004)
    data = np.random.randint(0, 255, size=(3, height, width)).astype("uint8")
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=3,
        dtype="uint8",
        transform=transform,
        # 故意不写 crs
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


def make_lines_gpkg() -> None:
    """X6：EPSG:3857 的 30 个 LineString GeoPackage，10 个属性列。"""
    import sqlite3
    import struct

    from shapely import to_wkb
    from shapely.geometry import LineString

    path = FIXTURES / "lines.gpkg"
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA application_id = 1196444487")
    conn.execute("PRAGMA user_version = 10200")
    conn.executescript(
        """
        CREATE TABLE gpkg_spatial_ref_sys (
            srs_name TEXT NOT NULL,
            srs_id INTEGER NOT NULL PRIMARY KEY,
            organization TEXT NOT NULL,
            organization_coordsys_id INTEGER NOT NULL,
            definition TEXT NOT NULL,
            description TEXT
        );
        CREATE TABLE gpkg_contents (
            table_name TEXT NOT NULL PRIMARY KEY,
            data_type TEXT NOT NULL,
            identifier TEXT UNIQUE,
            description TEXT DEFAULT '',
            last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE,
            srs_id INTEGER
        );
        CREATE TABLE gpkg_geometry_columns (
            table_name TEXT NOT NULL,
            column_name TEXT NOT NULL,
            geometry_type_name TEXT NOT NULL,
            srs_id INTEGER NOT NULL,
            z TINYINT NOT NULL,
            m TINYINT NOT NULL,
            CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name)
        );
        CREATE TABLE lines (
            fid INTEGER PRIMARY KEY AUTOINCREMENT,
            geom BLOB,
            name TEXT,
            length_m INTEGER,
            kind TEXT,
            group_id INTEGER,
            source TEXT,
            year INTEGER,
            code TEXT,
            flag INTEGER,
            weight REAL,
            remark TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)",
        (
            "Web Mercator",
            3857,
            "EPSG",
            3857,
            'PROJCS["WGS 84 / Pseudo-Mercator"]',
            "EPSG:3857",
        ),
    )
    conn.execute(
        "INSERT INTO gpkg_contents(table_name,data_type,identifier,min_x,min_y,max_x,max_y,srs_id) "
        "VALUES ('lines','features','lines',12690000,3500000,12770000,3580000,3857)"
    )
    conn.execute(
        "INSERT INTO gpkg_geometry_columns VALUES ('lines','geom','LINESTRING',3857,0,0)"
    )
    for i in range(30):
        x0 = 12690000 + (i % 6) * 10000
        y0 = 3500000 + (i // 6) * 10000
        geom = LineString([(x0, y0), (x0 + 8000, y0 + 4000), (x0 + 16000, y0)])
        header = b"GP" + bytes((0, 0x01)) + struct.pack("<i", 3857)
        blob = header + to_wkb(geom, hex=False, include_srid=False)
        conn.execute(
            "INSERT INTO lines(geom,name,length_m,kind,group_id,source,year,"
            "code,flag,weight,remark) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                blob,
                f"line-{i:02d}",
                24000,
                None,
                i // 6,
                "fixture",
                2026,
                f"L{i:02d}",
                i % 2,
                1.5 + i,
                f"remark-{i}",
            ),
        )
    conn.commit()
    conn.close()
    print(f"written {path}")


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
    make_lines_gpkg()
    make_attachment()
