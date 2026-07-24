"""OrionsBrain ↔ Nebula 私有 HTTP 兼容路由。

五组 POST 保留旧 desire 合同；六组 GET 只读取共享运行时，不提供写操作。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tools import breath as _tool_breath
from tools import dream as _tool_dream
from tools import hold as _tool_hold
from tools import trace as _tool_trace
from utils import strip_wikilinks
from web import _shared as sh

from .contracts import (
    bounded_query_int,
    check_desire_secret,
    coerce_bool,
    coerce_number,
    extract_bucket_id,
    normalize_tags,
    read_json_object_or_empty,
    unauthorized,
)


_EXCLUDED_TYPES = ("letter", "i")


def _taipei_today() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def _bucket_brief(bucket: dict) -> dict:
    metadata = bucket.get("metadata", {}) or {}
    return {
        "id": bucket["id"],
        "name": metadata.get("name", bucket["id"]),
        "type": metadata.get("type", "dynamic"),
        "domain": metadata.get("domain", []),
        "importance": metadata.get("importance", 5),
        "pinned": bool(metadata.get("pinned", False)),
        "valence": metadata.get("valence", 0.5),
        "arousal": metadata.get("arousal", 0.3),
        "created": metadata.get("created", ""),
        "last_active": metadata.get("last_active", ""),
        "score": sh.decay_engine.calculate_score(metadata),
        "content_preview": strip_wikilinks(bucket.get("content", ""))[:200],
    }


async def _active_buckets() -> list[dict]:
    buckets = await sh.bucket_mgr.list_all(include_archive=False)
    return [
        bucket
        for bucket in buckets
        if not (bucket.get("metadata", {}) or {}).get("deleted_at")
    ]


def register(mcp) -> None:
    @mcp.custom_route("/api/desire/recall", methods=["POST"])
    async def desire_recall(request: Request) -> Response:
        if not check_desire_secret(request):
            return unauthorized()
        body = await read_json_object_or_empty(request)
        arousal_min = coerce_number(body, "arousal_min", 0.85, float)
        valence_min = coerce_number(body, "valence_min", 0.6, float)
        limit = max(1, min(coerce_number(body, "limit", 2, int), 10))
        query = str(body.get("query") or "").strip().lower()
        try:
            buckets = await sh.bucket_mgr.list_all(include_archive=True)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

        hot: list[tuple[float, dict]] = []
        for bucket in buckets:
            metadata = bucket.get("metadata", {}) or {}
            try:
                arousal = float(metadata.get("arousal", 0.0))
                valence = float(metadata.get("valence", 0.5))
            except (TypeError, ValueError, OverflowError):
                arousal, valence = 0.0, 0.5
            if arousal >= arousal_min and valence >= valence_min:
                hot.append((arousal, bucket))

        if query:
            filtered = [
                (arousal, bucket)
                for arousal, bucket in hot
                if query in str(bucket.get("content", "") or "").lower()
                or query
                in str(
                    (bucket.get("metadata", {}) or {}).get("name", "") or ""
                ).lower()
            ]
            if filtered:
                hot = filtered

        hot.sort(key=lambda item: item[0], reverse=True)
        memories = []
        for arousal, bucket in hot[:limit]:
            metadata = bucket.get("metadata", {}) or {}
            memories.append(
                {
                    "id": bucket.get("id", ""),
                    "name": metadata.get("name", ""),
                    "valence": metadata.get("valence"),
                    "arousal": metadata.get("arousal"),
                    "preview": strip_wikilinks(
                        str(bucket.get("content", "") or "")
                    )[:200],
                }
            )
        return JSONResponse(
            {"ok": True, "count": len(memories), "memories": memories}
        )

    @mcp.custom_route("/api/desire/breath", methods=["POST"])
    async def desire_breath(request: Request) -> Response:
        if not check_desire_secret(request):
            return unauthorized()
        body = await read_json_object_or_empty(request)
        try:
            text = await _tool_breath.dispatch(
                query=str(body.get("query") or ""),
                max_tokens=coerce_number(body, "max_tokens", 0, int),
                domain=str(body.get("domain") or ""),
                valence=coerce_number(body, "valence", -1, float),
                arousal=coerce_number(body, "arousal", -1, float),
                max_results=coerce_number(body, "max_results", 0, int),
                importance_min=coerce_number(
                    body, "importance_min", -1, int
                ),
                tags=normalize_tags(body.get("tags")),
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        return JSONResponse({"ok": True, "text": text})

    @mcp.custom_route("/api/desire/hold", methods=["POST"])
    async def desire_hold(request: Request) -> Response:
        if not check_desire_secret(request):
            return unauthorized()
        body = await read_json_object_or_empty(request)
        content = str(body.get("content") or "").strip()
        if not content:
            return JSONResponse({"error": "content required"}, status_code=400)
        landing_receipt: dict = {}
        try:
            text = await _tool_hold.dispatch(
                content=content,
                tags=normalize_tags(body.get("tags")),
                importance=coerce_number(body, "importance", 5, int),
                pinned=coerce_bool(body.get("pinned"), False),
                feel=coerce_bool(body.get("feel"), False),
                source_bucket=str(body.get("source_bucket") or ""),
                valence=coerce_number(body, "valence", -1, float),
                arousal=coerce_number(body, "arousal", -1, float),
                why_remembered=str(body.get("why_remembered") or ""),
                _receipt_out=landing_receipt,
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        payload = {"ok": True, "text": text}
        bucket_id = extract_bucket_id(text)
        if bucket_id:
            payload["bucket_id"] = bucket_id
        if landing_receipt:
            payload["receipt"] = landing_receipt
        return JSONResponse(payload)

    @mcp.custom_route("/api/desire/dream", methods=["POST"])
    async def desire_dream(request: Request) -> Response:
        if not check_desire_secret(request):
            return unauthorized()
        body = await read_json_object_or_empty(request)
        window_hours = coerce_number(body, "window_hours", 48, int)
        try:
            text = await _tool_dream.dispatch(window_hours=window_hours)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        return JSONResponse({"ok": True, "text": text})

    @mcp.custom_route("/api/desire/trace", methods=["POST"])
    async def desire_trace(request: Request) -> Response:
        if not check_desire_secret(request):
            return unauthorized()
        body = await read_json_object_or_empty(request)
        bucket_id = str(body.get("bucket_id") or "").strip()
        if not bucket_id:
            return JSONResponse(
                {"error": "bucket_id required"}, status_code=400
            )
        try:
            text = await _tool_trace.dispatch(
                bucket_id=bucket_id,
                name=str(body.get("name") or ""),
                domain=str(body.get("domain") or ""),
                valence=coerce_number(body, "valence", -1, float),
                arousal=coerce_number(body, "arousal", -1, float),
                importance=coerce_number(body, "importance", -1, int),
                tags=normalize_tags(body.get("tags")),
                resolved=coerce_number(body, "resolved", -1, int),
                pinned=coerce_number(body, "pinned", -1, int),
                digested=coerce_number(body, "digested", -1, int),
                content=str(body.get("content") or ""),
                delete=False,
                status=str(body.get("status") or ""),
                weight=coerce_number(body, "weight", -1, float),
                dont_surface=coerce_number(
                    body, "dont_surface", -1, int
                ),
                why_remembered=str(body.get("why_remembered") or ""),
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        return JSONResponse({"ok": True, "text": text})

    @mcp.custom_route("/api/desire/buckets", methods=["GET"])
    async def desire_buckets(request: Request) -> Response:
        if not check_desire_secret(request):
            return unauthorized()
        filter_mode = request.query_params.get("filter", "all")
        sort_mode = request.query_params.get("sort", "score")
        limit = bounded_query_int(request, "limit", 30)
        offset = bounded_query_int(
            request, "offset", 0, minimum=0, maximum=1_000_000
        )
        try:
            rows = [
                bucket
                for bucket in await _active_buckets()
                if (bucket.get("metadata", {}) or {}).get("type")
                not in _EXCLUDED_TYPES
            ]
            if filter_mode == "pinned":
                rows = [
                    bucket
                    for bucket in rows
                    if (bucket.get("metadata", {}) or {}).get("pinned")
                ]
            elif filter_mode == "feel":
                rows = [
                    bucket
                    for bucket in rows
                    if (bucket.get("metadata", {}) or {}).get("type") == "feel"
                ]
            briefs = [_bucket_brief(bucket) for bucket in rows]
            if sort_mode == "recent":
                briefs.sort(
                    key=lambda item: item["created"] or "", reverse=True
                )
            else:
                briefs.sort(key=lambda item: item["score"], reverse=True)
            return JSONResponse(
                {
                    "total": len(briefs),
                    "buckets": briefs[offset : offset + limit],
                }
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    @mcp.custom_route(
        "/api/desire/bucket/{bucket_id}", methods=["GET"]
    )
    async def desire_bucket_detail(request: Request) -> Response:
        if not check_desire_secret(request):
            return unauthorized()
        bucket_id = request.path_params["bucket_id"]
        try:
            bucket = await sh.bucket_mgr.get(bucket_id)
            if not bucket:
                return JSONResponse(
                    {"error": "not found"}, status_code=404
                )
            result = _bucket_brief(bucket)
            metadata = bucket.get("metadata", {}) or {}
            result["content"] = strip_wikilinks(
                str(bucket.get("content", "") or "")
            )
            result["tags"] = metadata.get("tags", [])
            result["why_remembered"] = metadata.get(
                "why_remembered", ""
            )
            result["digested"] = bool(metadata.get("digested", False))
            return JSONResponse(result)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    @mcp.custom_route("/api/desire/letters-list", methods=["GET"])
    async def desire_letters(request: Request) -> Response:
        if not check_desire_secret(request):
            return unauthorized()
        limit = bounded_query_int(request, "limit", 30)
        offset = bounded_query_int(
            request, "offset", 0, minimum=0, maximum=1_000_000
        )
        try:
            letters = [
                bucket
                for bucket in await _active_buckets()
                if (bucket.get("metadata", {}) or {}).get("type") == "letter"
            ]
            letters.sort(
                key=lambda bucket: (
                    (bucket.get("metadata", {}) or {}).get("letter_date")
                    or (bucket.get("metadata", {}) or {}).get("created", "")
                ),
                reverse=True,
            )
            result = []
            for bucket in letters[offset : offset + limit]:
                metadata = bucket.get("metadata", {}) or {}
                result.append(
                    {
                        "id": bucket["id"],
                        "author": metadata.get("author", ""),
                        "title": metadata.get("title", "")
                        or metadata.get("name", ""),
                        "date": metadata.get("letter_date")
                        or metadata.get("created", "")[:10],
                        "content": strip_wikilinks(
                            str(bucket.get("content", "") or "")
                        ),
                    }
                )
            return JSONResponse(
                {"total": len(letters), "letters": result}
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    @mcp.custom_route("/api/desire/anchors-list", methods=["GET"])
    async def desire_anchors(request: Request) -> Response:
        if not check_desire_secret(request):
            return unauthorized()
        try:
            anchors = await sh.bucket_mgr.list_anchors()
            return JSONResponse(
                {
                    "anchors": [
                        _bucket_brief(bucket) for bucket in anchors
                    ]
                }
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    @mcp.custom_route("/api/desire/i-notes", methods=["GET"])
    async def desire_i_notes(request: Request) -> Response:
        if not check_desire_secret(request):
            return unauthorized()
        limit = bounded_query_int(request, "limit", 30)
        try:
            notes = [
                bucket
                for bucket in await _active_buckets()
                if (bucket.get("metadata", {}) or {}).get("type") == "i"
            ]
            notes.sort(
                key=lambda bucket: (
                    (bucket.get("metadata", {}) or {}).get("last_active", "")
                    or (bucket.get("metadata", {}) or {}).get("created", "")
                ),
                reverse=True,
            )
            result = []
            for bucket in notes[:limit]:
                metadata = bucket.get("metadata", {}) or {}
                aspect = next(
                    (
                        tag.split(":", 1)[1]
                        for tag in metadata.get("tags", [])
                        if isinstance(tag, str)
                        and tag.startswith("aspect:")
                    ),
                    "",
                )
                result.append(
                    {
                        "id": bucket["id"],
                        "aspect": aspect,
                        "created": metadata.get("created", ""),
                        "last_active": metadata.get("last_active", ""),
                        "content": strip_wikilinks(
                            str(bucket.get("content", "") or "")
                        ),
                    }
                )
            return JSONResponse({"notes": result})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    @mcp.custom_route("/api/desire/brain-stats", methods=["GET"])
    async def desire_brain_stats(request: Request) -> Response:
        if not check_desire_secret(request):
            return unauthorized()
        try:
            rows = await _active_buckets()
            memories = [
                bucket
                for bucket in rows
                if (bucket.get("metadata", {}) or {}).get("type")
                not in _EXCLUDED_TYPES
            ]
            today = _taipei_today()
            return JSONResponse(
                {
                    "total": len(memories),
                    "pinned": sum(
                        1
                        for bucket in memories
                        if (bucket.get("metadata", {}) or {}).get("pinned")
                    ),
                    "feel": sum(
                        1
                        for bucket in memories
                        if (bucket.get("metadata", {}) or {}).get("type")
                        == "feel"
                    ),
                    "today_new": sum(
                        1
                        for bucket in memories
                        if str(
                            (bucket.get("metadata", {}) or {}).get(
                                "created", ""
                            )
                        ).startswith(today)
                    ),
                    "letters": sum(
                        1
                        for bucket in rows
                        if (bucket.get("metadata", {}) or {}).get("type")
                        == "letter"
                    ),
                    "anchors": sum(
                        1
                        for bucket in memories
                        if (bucket.get("metadata", {}) or {}).get("anchor")
                    ),
                }
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
