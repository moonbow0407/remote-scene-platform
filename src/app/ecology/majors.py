"""内置生态参量大类。01–07 名称以本表为准。"""

from __future__ import annotations

import re

from app.errors import validation_error

ITEM_CODE_PATTERN = re.compile(r"^\d{4}$")
MAJOR_CODE_PATTERN = re.compile(r"^\d{2}$")
ABBREV_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

MAJORS: dict[str, str] = {
    "01": "生物参数",
    "02": "土壤参数",
    "03": "大气参数",
    "04": "水文地质参数",
    "05": "开采相关参数",
    "06": "双碳参数",
    "07": "水体参数",
}


def major_code_of(item_code: str) -> str:
    if not ITEM_CODE_PATTERN.fullmatch(item_code):
        raise validation_error("细项编号须为 4 位数字，例如 0102")
    return item_code[:2]


def resolve_major_name(major_code: str, provided: str | None) -> str:
    if not MAJOR_CODE_PATTERN.fullmatch(major_code):
        raise validation_error("大类编号须为 2 位数字，例如 01")
    builtin = MAJORS.get(major_code)
    name = None if provided is None else provided.strip()
    if builtin is not None:
        if name and name != builtin:
            raise validation_error(f"大类 {major_code} 的名称必须是「{builtin}」")
        return builtin
    if not name:
        raise validation_error(f"未知大类 {major_code} 必须提供 major_name")
    return name
