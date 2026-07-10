"""
========================================
web/daemon_ro.py — nebula-daemon 专用只读口（0710 · Nebula 大脑 dashboard 批）
========================================

给 LA 的 nebula-daemon 开的一组 GET 只读路由，喂 Nebula app 里的"OrionsBrain"
dashboard（ORION tab）。与 /api/desire/* 同族：

- 鉴权 = X-Ombre-Secret 头（同 breath/hold 口，_check_secret 逻辑照抄 server.py），
  与 dashboard 的 cookie session 完全无关——daemon 程序化调用，不碰登录态。
- 取数 = 直调 sh.bucket_mgr（dashboard 同一单例），JSON 结构化返回——
  不走 MCP dispatch（那是给模型看的格式化文本，机器侧还得反解析）。
- 全部只读：本模块一个写操作都没有（红线：LA 对 Ombre 的写只有 hold 口那一条）。

路由：
  GET /api/desire/buckets       ?filter=all|pinned|feel&sort=score|recent&limit=&offset=
  GET /api/desire/bucket/{id}   单桶全文
  GET /api/desire/letters-list  ?limit=&offset=
  GET /api/desire/anchors-list
  GET /api/desire/i-notes       ?limit=
  GET /api/desire/brain-stats   计数卡（总桶/钉选/feel/今日新增/信/锚点）
========================================
"""

import hmac
import os
from datetime import datetime, timedelta, timezone

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh

try:
    from utils import strip_wikilinks  # type: ignore
except ImportError:  # pragma: no cover
    from ..utils import strip_wikilinks  # type: ignore

# 记忆桶列表里不掺这两类：信有自己的 tab，i 是 dont_surface 的自我认知流
_EXCLUDED_TYPES = ("letter", "i")


def _check_secret(request: Request) -> bool:
    """逐字照抄 server.py 的 _check_desire_secret（含两侧 .strip()——Zeabur 的
    env 值可能带尾随空白，0710 就栽在没 strip 上：401 了一轮）。"""
    expected = (os.environ.get("OMBRE_DESIRE_TOKEN", "") or "").strip()
    if not expected:
        return False
    got = (request.headers.get("x-ombre-secret") or "").strip()
    return bool(got) and hmac.compare_digest(got, expected)


def _deny():
    from starlette.responses import JSONResponse
    return JSONResponse({"error": "Unauthorized"}, status_code=401)


def _taipei_today() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def _bucket_brief(b: dict) -> dict:
    meta = b.get("metadata", {})
    return {
        "id": b["id"],
        "name": meta.get("name", b["id"]),
        "type": meta.get("type", "dynamic"),
        "domain": meta.get("domain", []),
        "importance": meta.get("importance", 5),
        "pinned": bool(meta.get("pinned", False)),
        "valence": meta.get("valence", 0.5),
        "arousal": meta.get("arousal", 0.3),
        "created": meta.get("created", ""),
        "last_active": meta.get("last_active", ""),
        "score": sh.decay_engine.calculate_score(meta),
        "content_preview": strip_wikilinks(b.get("content", ""))[:200],
    }


async def _active_buckets() -> list:
    all_b = await sh.bucket_mgr.list_all(include_archive=False)
    return [b for b in all_b if not b.get("metadata", {}).get("deleted_at")]


def register(mcp) -> None:

    @mcp.custom_route("/api/desire/buckets", methods=["GET"])
    async def ro_buckets(request: Request) -> Response:
        from starlette.responses import JSONResponse
        if not _check_secret(request):
            return _deny()
        flt = request.query_params.get("filter", "all")
        sort = request.query_params.get("sort", "score")
        limit = min(int(request.query_params.get("limit", "30") or 30), 100)
        offset = max(int(request.query_params.get("offset", "0") or 0), 0)
        try:
            rows = [b for b in await _active_buckets()
                    if b.get("metadata", {}).get("type") not in _EXCLUDED_TYPES]
            if flt == "pinned":
                rows = [b for b in rows if b["metadata"].get("pinned")]
            elif flt == "feel":
                rows = [b for b in rows if b["metadata"].get("type") == "feel"]
            briefs = [_bucket_brief(b) for b in rows]
            if sort == "recent":
                briefs.sort(key=lambda x: x["created"] or "", reverse=True)
            else:
                briefs.sort(key=lambda x: x["score"], reverse=True)
            return JSONResponse({"total": len(briefs), "buckets": briefs[offset:offset + limit]})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/api/desire/bucket/{bucket_id}", methods=["GET"])
    async def ro_bucket_detail(request: Request) -> Response:
        from starlette.responses import JSONResponse
        if not _check_secret(request):
            return _deny()
        bucket_id = request.path_params["bucket_id"]
        try:
            b = await sh.bucket_mgr.get(bucket_id)
            if not b:
                return JSONResponse({"error": "not found"}, status_code=404)
            out = _bucket_brief(b)
            out["content"] = strip_wikilinks(b.get("content", ""))
            out["tags"] = b.get("metadata", {}).get("tags", [])
            out["why_remembered"] = b.get("metadata", {}).get("why_remembered", "")
            out["digested"] = bool(b.get("metadata", {}).get("digested", False))
            return JSONResponse(out)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/api/desire/letters-list", methods=["GET"])
    async def ro_letters(request: Request) -> Response:
        from starlette.responses import JSONResponse
        if not _check_secret(request):
            return _deny()
        limit = min(int(request.query_params.get("limit", "30") or 30), 100)
        offset = max(int(request.query_params.get("offset", "0") or 0), 0)
        try:
            letters = [b for b in await _active_buckets()
                       if b["metadata"].get("type") == "letter"]
            letters.sort(
                key=lambda b: b["metadata"].get("letter_date") or b["metadata"].get("created", ""),
                reverse=True,
            )
            result = []
            for b in letters[offset:offset + limit]:
                m = b["metadata"]
                result.append({
                    "id": b["id"],
                    "author": m.get("author", ""),
                    "title": m.get("title", "") or m.get("name", ""),
                    "date": m.get("letter_date") or m.get("created", "")[:10],
                    "content": strip_wikilinks(b.get("content", "")),
                })
            return JSONResponse({"total": len(letters), "letters": result})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/api/desire/anchors-list", methods=["GET"])
    async def ro_anchors(request: Request) -> Response:
        from starlette.responses import JSONResponse
        if not _check_secret(request):
            return _deny()
        try:
            anchors = await sh.bucket_mgr.list_anchors()
            return JSONResponse({"anchors": [_bucket_brief(b) for b in anchors]})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/api/desire/i-notes", methods=["GET"])
    async def ro_i_notes(request: Request) -> Response:
        from starlette.responses import JSONResponse
        if not _check_secret(request):
            return _deny()
        limit = min(int(request.query_params.get("limit", "30") or 30), 100)
        try:
            notes = [b for b in await _active_buckets()
                     if b["metadata"].get("type") == "i"]
            notes.sort(key=lambda b: b["metadata"].get("last_active", "") or b["metadata"].get("created", ""), reverse=True)
            result = []
            for b in notes[:limit]:
                m = b["metadata"]
                aspect = next((t.split(":", 1)[1] for t in m.get("tags", [])
                               if isinstance(t, str) and t.startswith("aspect:")), "")
                result.append({
                    "id": b["id"],
                    "aspect": aspect,
                    "created": m.get("created", ""),
                    "last_active": m.get("last_active", ""),
                    "content": strip_wikilinks(b.get("content", "")),
                })
            return JSONResponse({"notes": result})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/api/desire/brain-stats", methods=["GET"])
    async def ro_brain_stats(request: Request) -> Response:
        from starlette.responses import JSONResponse
        if not _check_secret(request):
            return _deny()
        try:
            rows = await _active_buckets()
            mem = [b for b in rows if b["metadata"].get("type") not in _EXCLUDED_TYPES]
            today = _taipei_today()
            return JSONResponse({
                "total": len(mem),
                "pinned": sum(1 for b in mem if b["metadata"].get("pinned")),
                "feel": sum(1 for b in mem if b["metadata"].get("type") == "feel"),
                "today_new": sum(1 for b in mem if str(b["metadata"].get("created", "")).startswith(today)),
                "letters": sum(1 for b in rows if b["metadata"].get("type") == "letter"),
                "anchors": sum(1 for b in mem if b["metadata"].get("anchor")),
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
