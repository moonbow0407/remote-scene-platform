"""OpenAPI 导出给 Apifox 用的形状：3.0 nullable、单一 Bearer、空 Query 合法。"""

from app.openapi_compat import polish_openapi


def test_collapse_optional_enum_and_int() -> None:
    polished = polish_openapi(
        {
            "openapi": "3.1.0",
            "components": {"securitySchemes": {"HTTPBearer": {"type": "http", "scheme": "bearer"}}},
            "paths": {
                "/api/v1/satellites": {
                    "get": {
                        "security": [{"HTTPBearer": []}],
                        "parameters": [
                            {
                                "name": "data_source_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                                    "title": "Category Id",
                                },
                            },
                            {
                                "name": "status",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [
                                        {"$ref": "#/components/schemas/AssetStatus"},
                                        {"type": "null"},
                                    ]
                                },
                            },
                            {
                                "name": "page",
                                "in": "query",
                                "required": False,
                                "schema": {"type": "integer", "ge": 1, "le": 200, "default": 1},
                            },
                        ],
                    }
                }
            },
        }
    )
    assert polished["openapi"] == "3.0.3"
    assert "HTTPBearer" not in polished["components"]["securitySchemes"]
    assert polished["components"]["securitySchemes"]["BearerAuth"]["scheme"] == "bearer"
    operation = polished["paths"]["/api/v1/satellites"]["get"]
    params = {item["name"]: item for item in operation["parameters"]}
    assert params["data_source_id"]["allowEmptyValue"] is True
    assert params["data_source_id"]["schema"]["type"] == "integer"
    assert params["data_source_id"]["schema"]["nullable"] is True
    assert "anyOf" not in params["data_source_id"]["schema"]
    assert params["status"]["schema"]["allOf"] == [{"$ref": "#/components/schemas/AssetStatus"}]
    assert params["status"]["schema"]["nullable"] is True
    assert params["page"]["schema"]["minimum"] == 1
    assert params["page"]["schema"]["maximum"] == 200
    assert "ge" not in params["page"]["schema"]
    assert operation["security"] == [{"BearerAuth": []}]


def test_app_openapi_is_apifox_friendly() -> None:
    from app.api.app import create_app
    from app.settings import get_settings

    get_settings.cache_clear()
    schema = create_app().openapi()
    assert schema["openapi"].startswith("3.0")
    assert "HTTPBearer" not in schema["components"]["securitySchemes"]
    params = {
        item["name"]: item for item in schema["paths"]["/api/v1/satellites"]["get"]["parameters"]
    }
    assert "anyOf" not in params["data_source_id"]["schema"]
    assert params["data_source_id"]["allowEmptyValue"] is True
    assert params["status"]["schema"]["nullable"] is True
    search = schema["components"]["schemas"]["SearchRequest"]
    spatial = search["properties"]["spatial_geojson"]
    assert spatial.get("example") is None
    assert "/api/v1/assets" not in schema["paths"]
    assert "/api/v1/categories" not in schema["paths"]
    security = schema["paths"]["/api/v1/satellites"]["get"].get("security", schema["security"])
    assert security == [{"BearerAuth": []}]
    assert schema["paths"]["/api/v1/auth/login"]["post"]["security"] == []
    assert "get" in schema["paths"]["/api/v1/ecology/parameters/tree"]
    assert "get" in schema["paths"]["/api/v1/ecology/majors"]
    assert "get" in schema["paths"]["/api/v1/data-sources"]
    assert "post" in schema["paths"]["/api/v1/data/search"]
    assert "get" in schema["paths"]["/api/v1/mines"]
    assert "post" in schema["paths"]["/api/v1/mines"]
    assert "get" in schema["paths"]["/api/v1/mines/{mine_id}"]
    assert "get" in schema["paths"]["/api/v1/ecology/data-source-mappings"]
    assert "deleted" not in params
    assert "/api/v1/satellites/{record_id}/restore" not in schema["paths"]
