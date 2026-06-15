# ============================================================
# MCP transport auth wrapper / MCP 通道鉴权包装
#
# Why this file exists / 为什么有这个文件：
#   server.py exposes the native MCP endpoint (/mcp) via FastMCP's
#   streamable_http_app() with ONLY a CORS middleware — no auth. So anyone
#   who knows the URL can call breath/hold/grow/trace and read or modify all
#   memories. The dashboard (_require_auth) and the REST mirror
#   (/api/*, _check_api_auth) are already protected; only /mcp was left open.
#
#   This wrapper closes that hole WITHOUT editing server.py, so the fork can
#   still merge upstream (P0luz/Ombre-Brain) cleanly — it only adds a new file.
#   It reuses the existing OMBRE_API_TOKEN (the same token the Telegram bot and
#   the REST endpoints already use), so all clients share one credential.
#
# How to run (set as the start command, e.g. in Zeabur service settings):
#   uvicorn mcp_auth:app --host 0.0.0.0 --port 8000
# ============================================================

import hmac
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

# Importing server runs its module-level setup (config, engines, MCP tools,
# dashboard/REST routes) — same as launching it, minus the __main__ block.
from server import mcp, _verify_any_password

OMBRE_API_TOKEN = os.environ.get("OMBRE_API_TOKEN", "").strip()

# Paths that carry the raw MCP transport and must require a token.
# Everything else (/, /health, /auth/*, /dashboard, /api/*) keeps its own
# auth (or is intentionally public) and is left untouched.
_PROTECTED_PREFIXES = ("/mcp", "/sse")


def _token_ok(token: str) -> bool:
    """Mirror server._check_api_auth: accept OMBRE_API_TOKEN or the dashboard password."""
    if not token:
        return False
    if OMBRE_API_TOKEN and hmac.compare_digest(token, OMBRE_API_TOKEN):
        return True
    try:
        return _verify_any_password(token)
    except Exception:
        return False


class MCPAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        protected = any(
            path == p or path.startswith(p + "/") for p in _PROTECTED_PREFIXES
        )
        # Let CORS preflight through; the CORS middleware (outer) answers it.
        if protected and request.method != "OPTIONS":
            auth = request.headers.get("authorization", "")
            if auth[:7].lower() == "bearer ":
                token = auth[7:].strip()
            else:
                token = request.headers.get("x-api-token", "").strip()
            if not _token_ok(token):
                return JSONResponse(
                    {"error": "Unauthorized: MCP endpoint requires a valid token"},
                    status_code=401,
                )
        return await call_next(request)


# Build the same ASGI app server.py would, then layer auth under CORS.
# add_middleware wraps last-added outermost, so CORS stays outermost (handles
# preflight + adds headers even to 401s) and auth runs just inside it.
app = mcp.streamable_http_app()
app.add_middleware(MCPAuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)
