"""生态参量细项种子。code 为细项编号；不含 0204（空号）；0408 为土壤湿度。"""

from __future__ import annotations

from typing import TypedDict

from app.ecology.majors import ITEM_CODE_PATTERN, MAJORS, major_code_of


class SeedItem(TypedDict):
    code: str
    abbrev: str
    name: str
    english_name: str | None
    major_code: str
    major_name: str


# (code, abbrev, name, english_name)
_ROWS: tuple[tuple[str, str, str, str | None], ...] = (
    ("0101", "SAVI", "土壤植被调节指数", "Soil Adjusted Vegetation Index"),
    ("0102", "NDVI", "归一化植被指数", "Normalized Difference Vegetation Index"),
    ("0103", "EVI", "增强型植被指数", "Enhanced Vegetation Index"),
    ("0104", "PD", "植物多样性", "Plant Diversity"),
    ("0105", "Biomass", "生物量", "Biomass"),
    ("0106", "Chl", "叶绿素含量", "Chlorophyll"),
    ("0107", "BP", "褐色素含量", "Brown Pigments"),
    ("0108", "LAI", "叶面积指数", "Leaf Area Index"),
    ("0109", "VCDI", "乔灌草均质性指数", None),
    ("0110", "SWIRVI", "短波红外植被指数", "Shortwave Infrared Vegetation Index"),
    ("0111", "FVC", "植被覆盖度", "Fractional Vegetation Cover"),
    ("0112", "MSI", "水分胁迫指数", "Moisture Stress Index"),
    ("0114", "GCI", "绿度指数", "Greenness Index"),
    ("0115", "CD", "郁闭度", "Canopy Density"),
    ("0116", "CWC", "冠状含水率", "Canopy Water Content"),
    ("0117", "RSEI", "遥感生态指数", None),
    ("0118", "SASDI", "半干旱草原荒漠化指数", None),
    ("0119", "NDRE", "归一化差值红边指数", None),
    ("0120", "IRECI", "反转红边叶绿素指数", None),
    ("0121", "MCARI2", "改进型叶绿素吸收反射率指数", None),
    ("0201", "Roughness", "粗糙度", "Roughness"),
    ("0202", "LST", "地表温度", "Land Surface Temperature"),
    ("0203", "BSI", "裸土指数", "Bare Soil Index"),
    ("0205", "pH", "土壤PH值", "pH"),
    ("0206", "SOM", "土壤有机质含量", "Soil Organic Matter"),
    ("0207", "TN", "全氮", "Total Nitrogen"),
    ("0208", "AP", "有效磷", "Available Phosphorus"),
    ("0209", "AK", "速效钾", "Available Potassium"),
    ("0301", "DUST", "滞尘量", None),
    ("0305", "TEMP", "地上温度", None),
    ("0306", "PRCP", "降雨量", None),
    ("0307", "SR", "太阳辐射", None),
    ("0401", "WVP", "大气水汽含量", None),
    ("0402", "TWI", "地形湿度指数", None),
    ("0403", "Slope", "坡度", "Slope"),
    ("0404", "Aspect", "坡向", "Aspect"),
    ("0405", "RUSLE", "土壤侵蚀量", "Soil Erosion"),
    ("0406", "SHCI", "地表水力连通性指数", None),
    ("0407", "GD", "侵蚀沟密度", "Gully Density"),
    ("0408", "SM", "土壤湿度", "Soil Moisture"),
    ("0501", "GT", "岩土体类型", "Geotechnical Type"),
    ("0502", "S-M-D-R", "剥采排复", "S-M-D-R"),
    ("0505", "LULC", "土地利用类型", None),
    ("0506", "NDCMI", "归一化差异煤矿指数", None),
    ("0601", "NPP", "净初级生产力", None),
    ("0605", "SOC", "土壤有机碳", None),
    ("0701", "Water", "水体面积", "Water"),
    ("0706", "NDWI", "归一化差分水体指数", None),
)


def seed_items() -> list[SeedItem]:
    items: list[SeedItem] = []
    for code, abbrev, name, english_name in _ROWS:
        major_code = major_code_of(code)
        items.append(
            {
                "code": code,
                "abbrev": abbrev,
                "name": name,
                "english_name": english_name,
                "major_code": major_code,
                "major_name": MAJORS[major_code],
            }
        )
    return items


def assert_seed_invariants() -> None:
    items = seed_items()
    if len(items) != 48:
        raise AssertionError(f"种子应为 48 条，实际 {len(items)}")
    codes = [item["code"] for item in items]
    abbrevs = [item["abbrev"] for item in items]
    if "0204" in codes:
        raise AssertionError("0204 必须空号，不得入库")
    if len(set(codes)) != len(codes):
        raise AssertionError("细项编号重复")
    if len(set(abbrevs)) != len(abbrevs):
        raise AssertionError("英文缩写重复")
    for item in items:
        if not ITEM_CODE_PATTERN.fullmatch(item["code"]):
            raise AssertionError(f"非法细项编号 {item['code']}")
        if item["major_code"] != item["code"][:2]:
            raise AssertionError(f"{item['code']} 与大类 {item['major_code']} 不一致")
        if item["major_name"] != MAJORS[item["major_code"]]:
            raise AssertionError(f"{item['code']} 大类名称不正确")
