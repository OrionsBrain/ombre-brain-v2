"""Nebula 私有 HTTP 合同的输入、鉴权与回执辅助函数。"""

from __future__ import annotations

import hmac
import os
import re
from typing import Any, Callable, TypeVar

from starlette.requests import Request
from starlette.responses import JSONResponse


_Number = TypeVar("_Number", int, float)
_RECEIPT_ID = re.compile(r"(?:新建|合并|钉选|feel)→([^\s]+)")


def check_desire_secret(request: Request) -> bool:
    """校验 daemon 专用 secret；未配置服务端 secret 时失败锁死。"""
    expected = (os.environ.get("OMBRE_DESIRE_TOKEN", "") or "").strip()
    if not expected:
        return False
    received = (request.headers.get("x-ombre-secret") or "").strip()
    return bool(received) and hmac.compare_digest(received, expected)


def unauthorized() -> JSONResponse:
    return JSONResponse({"error": "Unauthorized"}, status_code=401)


async def read_json_object_or_empty(request: Request) -> dict[str, Any]:
    """兼容旧入口：空体、损坏 JSON 或非对象 JSON 都按空对象处理。"""
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def coerce_number(
    body: dict[str, Any],
    key: str,
    default: _Number,
    cast: Callable[[Any], _Number],
) -> _Number:
    try:
        return cast(body.get(key, default))
    except (TypeError, ValueError, OverflowError):
        return default


def coerce_bool(value: Any, default: bool = False) -> bool:
    """解析 JSON 布尔值，同时避免字符串 ``"false"`` 被当成真。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return default


def normalize_tags(value: Any) -> str:
    """把 Nebula bridge 的标签数组转成 Ombre 工具使用的逗号字符串。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ",".join(
            text
            for item in value
            if (text := str(item).strip())
        )
    return str(value).strip()


def bounded_query_int(
    request: Request,
    key: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int = 100,
) -> int:
    try:
        value = int(request.query_params.get(key, str(default)) or default)
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(minimum, min(value, maximum))


def extract_bucket_id(receipt: Any) -> str:
    """从旧中文文本回执中提取 bucket ID，供新结构化字段渐进迁移。"""
    match = _RECEIPT_ID.search(str(receipt or ""))
    return match.group(1) if match else ""
