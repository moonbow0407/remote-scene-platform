"""GeoJSON 流式解析（仅标准库）。

不能用 json.loads(整个文件)：FeatureCollection 可达数十 GB，整文件物化会打爆 Worker。
本模块按块读取，只把当前正在解析的单个 JSON 值留在缓冲区。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TextIO

from app.processing.errors import DeterministicError

_CHUNK_SIZE = 1024 * 1024
_WS = " \t\r\n"
_FEATURE_KEEP_KEYS = frozenset({"type", "geometry", "properties", "id", "bbox"})


def peek_geojson_root_type(path: Path) -> str:
    """只读取顶层 type（或先出现的 features）判定根对象种类，不扫描要素数组。"""
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            stream = JsonStream(handle)
            if stream.peek() != "{":
                raise DeterministicError(
                    "INVALID_VECTOR_ARCHIVE", "GeoJSON 必须是 FeatureCollection 或 Feature"
                )
            stream.consume("{")
            while True:
                stream.skip_ws()
                if stream.peek() == "}":
                    break
                key = stream.read_json_value()
                if not isinstance(key, str):
                    raise DeterministicError("INVALID_VECTOR_ARCHIVE", "不是合法 GeoJSON 文本")
                stream.consume(":")
                if key == "type":
                    value = stream.read_json_value()
                    if not isinstance(value, str):
                        raise DeterministicError(
                            "INVALID_VECTOR_ARCHIVE", "GeoJSON type 必须是字符串"
                        )
                    return value
                if key == "features":
                    # 顶层已出现 features，按 FeatureCollection 处理，避免为探测扫描整个数组
                    return "FeatureCollection"
                stream.skip_value()
                stream.skip_ws()
                if stream.peek() == ",":
                    stream.consume(",")
            raise DeterministicError(
                "INVALID_VECTOR_ARCHIVE", "GeoJSON 必须是 FeatureCollection 或 Feature"
            )
    except (OSError, UnicodeDecodeError) as exc:
        raise DeterministicError("INVALID_VECTOR_ARCHIVE", "不是合法 GeoJSON 文本") from exc


def iter_geojson_feature_objects(path: Path) -> Iterator[dict[str, Any]]:
    """逐个产出 Feature 对象；单要素 Feature 根对象也支持。"""
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            yield from _iter_feature_objects(JsonStream(handle))
    except (OSError, UnicodeDecodeError) as exc:
        raise DeterministicError("INVALID_VECTOR_ARCHIVE", "不是合法 GeoJSON 文本") from exc


def _iter_feature_objects(stream: JsonStream) -> Iterator[dict[str, Any]]:
    if stream.peek() != "{":
        raise DeterministicError(
            "INVALID_VECTOR_ARCHIVE", "GeoJSON 必须是 FeatureCollection 或 Feature"
        )
    stream.consume("{")
    collected: dict[str, Any] = {}
    features_seen = False
    while True:
        stream.skip_ws()
        if stream.peek() == "}":
            stream.consume("}")
            break
        key = stream.read_json_value()
        if not isinstance(key, str):
            raise DeterministicError("INVALID_VECTOR_ARCHIVE", "不是合法 GeoJSON 文本")
        stream.consume(":")
        if key == "features":
            for item in stream.iter_json_array():
                if not isinstance(item, dict) or item.get("type") != "Feature":
                    raise DeterministicError("INVALID_VECTOR_ARCHIVE", "GeoJSON 含非 Feature 成员")
                yield item
            features_seen = True
        elif key in _FEATURE_KEEP_KEYS:
            collected[key] = stream.read_json_value()
        else:
            stream.skip_value()
        stream.skip_ws()
        if stream.peek() == ",":
            stream.consume(",")

    root_type = collected.get("type")
    if features_seen:
        if root_type != "FeatureCollection":
            raise DeterministicError(
                "INVALID_VECTOR_ARCHIVE", "含 features 的 GeoJSON 必须是 FeatureCollection"
            )
        return
    if root_type == "Feature":
        yield collected
        return
    raise DeterministicError(
        "INVALID_VECTOR_ARCHIVE", "GeoJSON 必须是 FeatureCollection 或 Feature"
    )


class JsonStream:
    """块缓冲的 JSON 拉取解析器；缓冲区只保留当前未完成值。"""

    def __init__(self, handle: TextIO, *, chunk_size: int = _CHUNK_SIZE) -> None:
        self._handle = handle
        self._chunk_size = chunk_size
        self._buf = ""
        self._pos = 0
        self._eof = False
        self._decoder = json.JSONDecoder()

    def skip_ws(self) -> None:
        while True:
            while self._pos < len(self._buf):
                if self._buf[self._pos] not in _WS:
                    return
                self._pos += 1
            self._compact()
            if not self._fill():
                return

    def peek(self) -> str:
        self.skip_ws()
        self._ensure_char()
        return self._buf[self._pos]

    def consume(self, expected: str) -> None:
        self.skip_ws()
        self._ensure_len(len(expected))
        actual = self._buf[self._pos : self._pos + len(expected)]
        if actual != expected:
            raise DeterministicError(
                "INVALID_VECTOR_ARCHIVE", f"GeoJSON 语法错误：期望 {expected!r}"
            )
        self._pos += len(expected)

    def read_json_value(self) -> Any:
        self.skip_ws()
        self._compact()
        while True:
            try:
                value, end = self._decoder.raw_decode(self._buf, self._pos)
            except json.JSONDecodeError as exc:
                if self._fill():
                    continue
                raise DeterministicError("INVALID_VECTOR_ARCHIVE", "不是合法 GeoJSON 文本") from exc
            if self._number_may_continue(end, value) and self._fill():
                continue
            self._pos = end
            return value

    def iter_json_array(self) -> Iterator[Any]:
        self.consume("[")
        self.skip_ws()
        if self.peek() == "]":
            self.consume("]")
            return
        while True:
            yield self.read_json_value()
            self.skip_ws()
            ch = self.peek()
            if ch == "]":
                self.consume("]")
                return
            if ch != ",":
                raise DeterministicError("INVALID_VECTOR_ARCHIVE", "GeoJSON 数组语法错误")
            self.consume(",")

    def skip_value(self) -> None:
        self.skip_ws()
        ch = self.peek()
        if ch in "{[":
            self._skip_container()
            return
        self.read_json_value()

    def _skip_container(self) -> None:
        start = self._buf[self._pos]
        self._pos += 1
        depth = 1
        in_string = False
        escape = False
        while depth > 0:
            if self._pos >= len(self._buf):
                self._compact()
                if not self._fill():
                    raise DeterministicError("INVALID_VECTOR_ARCHIVE", "GeoJSON 意外结束")
            ch = self._buf[self._pos]
            self._pos += 1
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch in "{[":
                depth += 1
            elif ch in "}]":
                if (start == "{" and ch == "]") or (start == "[" and ch == "}"):
                    raise DeterministicError("INVALID_VECTOR_ARCHIVE", "GeoJSON 括号不匹配")
                depth -= 1

    def _number_may_continue(self, end: int, value: Any) -> bool:
        # raw_decode 在块边界会把尚未读完的数字当成完整值（"12" 后面还有 "345"）
        if self._eof or end < len(self._buf):
            return False
        return type(value) in (int, float)

    def _ensure_char(self) -> None:
        while self._pos >= len(self._buf):
            self._compact()
            if not self._fill():
                raise DeterministicError("INVALID_VECTOR_ARCHIVE", "GeoJSON 意外结束")

    def _ensure_len(self, size: int) -> None:
        while self._pos + size > len(self._buf):
            if not self._fill():
                raise DeterministicError("INVALID_VECTOR_ARCHIVE", "GeoJSON 意外结束")

    def _compact(self) -> None:
        if self._pos > 0:
            self._buf = self._buf[self._pos :]
            self._pos = 0

    def _fill(self) -> bool:
        if self._eof:
            return False
        chunk = self._handle.read(self._chunk_size)
        if not chunk:
            self._eof = True
            return False
        self._buf += chunk
        return True
