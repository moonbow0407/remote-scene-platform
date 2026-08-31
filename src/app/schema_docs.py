"""给枚举补 OpenAPI 中文标题和取值说明。取值本身仍是英文常量。"""

from collections.abc import Callable
from enum import StrEnum
from typing import TypeVar

from pydantic import GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

E = TypeVar("E", bound=type[StrEnum])


def enum_docs(title: str, meaning: str) -> Callable[[E], E]:
    def decorator(enum_cls: E) -> E:
        def json_schema(
            _cls: type[StrEnum],
            core_schema: CoreSchema,
            handler: GetJsonSchemaHandler,
        ) -> JsonSchemaValue:
            schema = handler(core_schema)
            schema["title"] = title
            schema["description"] = meaning
            return schema

        enum_cls.__get_pydantic_json_schema__ = classmethod(json_schema)  # type: ignore[method-assign, assignment]
        return enum_cls

    return decorator
