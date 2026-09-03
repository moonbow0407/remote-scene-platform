"""生态参量字典：种子不变量、校验与按大类分组。"""

from types import SimpleNamespace

from pydantic import ValidationError

from app.api.app import create_app
from app.ecology.enums import EcologicalParameterStatus
from app.ecology.majors import MAJORS, resolve_major_name
from app.ecology.schemas import EcologicalParameterCreate
from app.ecology.seed_data import assert_seed_invariants, seed_items
from app.ecology.service import group_parameters_by_major
from app.errors import ProblemError
from app.settings import get_settings


def test_seed_invariants() -> None:
    assert_seed_invariants()
    items = seed_items()
    assert {item["code"] for item in items} >= {"0102", "0112", "0207", "0406", "0408"}
    by_code = {item["code"]: item for item in items}
    assert by_code["0112"]["abbrev"] == "MSI"
    assert by_code["0207"]["abbrev"] == "TN"
    assert by_code["0406"]["abbrev"] == "SHCI"
    assert by_code["0408"]["name"] == "土壤湿度"
    assert by_code["0408"]["major_code"] == "04"


def test_create_rejects_old_abbrev_as_code() -> None:
    try:
        EcologicalParameterCreate(code="NDVI", name="归一化植被指数", abbrev="NDVI")
    except ValidationError as exc:
        assert any("0102" in str(err.get("msg", "")) or "4 位" in str(err) for err in exc.errors())
    else:
        raise AssertionError("NDVI 不能再当作 code")


def test_create_accepts_item_code() -> None:
    body = EcologicalParameterCreate(code="0102", name="归一化植被指数", abbrev="NDVI")
    assert body.code == "0102"
    assert body.abbrev == "NDVI"
    assert body.remark is None


def test_unknown_major_requires_name() -> None:
    try:
        resolve_major_name("08", None)
    except ProblemError as exc:
        assert exc.status == 422
    else:
        raise AssertionError("未知大类缺少名称应 422")
    assert resolve_major_name("08", "新大类") == "新大类"
    assert resolve_major_name("01", None) == MAJORS["01"]
    try:
        resolve_major_name("01", "别的名字")
    except ProblemError:
        pass
    else:
        raise AssertionError("内置大类名称不允许改")


def test_tree_groups_by_major_without_parent_id() -> None:
    rows = [
        SimpleNamespace(
            id=2,
            code="0201",
            abbrev="Roughness",
            name="粗糙度",
            english_name="Roughness",
            major_code="02",
            major_name="土壤参数",
            status=EcologicalParameterStatus.ACTIVE,
            sort_order=0,
            remark=None,
        ),
        SimpleNamespace(
            id=1,
            code="0102",
            abbrev="NDVI",
            name="归一化植被指数",
            english_name=None,
            major_code="01",
            major_name="生物参数",
            status=EcologicalParameterStatus.ACTIVE,
            sort_order=0,
            remark=None,
        ),
    ]
    tree = group_parameters_by_major(rows)  # type: ignore[arg-type]
    assert [node.major_code for node in tree] == ["01", "02"]
    assert tree[0].children[0].id == 1
    assert tree[0].children[0].code == "0102"
    assert "id" not in tree[0].model_dump()


def test_openapi_publishes_majors_and_new_tree() -> None:
    get_settings.cache_clear()
    schema = create_app().openapi()
    assert "get" in schema["paths"]["/api/v1/ecology/majors"]
    tree = schema["paths"]["/api/v1/ecology/parameters/tree"]["get"]
    ref = tree["responses"]["200"]["content"]["application/json"]["schema"]["items"]["$ref"]
    assert ref.endswith("EcologicalParameterMajorNode")
    props = schema["components"]["schemas"]["EcologicalParameterResponse"]["properties"]
    assert "parent_id" not in props
    assert "abbrev" in props
    assert "major_code" in props
