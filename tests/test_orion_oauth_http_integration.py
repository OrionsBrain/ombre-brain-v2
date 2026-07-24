"""隔离 HTTP 实例上的 OAuth DCR、PKCE、refresh 与 MCP 绑定验收。"""

from __future__ import annotations

import base64
import hashlib
import os
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest


BASE_URL = os.environ.get(
    "OMBRE_DOCKER_PUBLIC_OAUTH_URL", ""
).strip().rstrip("/")
PASSWORD = os.environ.get(
    "OMBRE_DOCKER_PUBLIC_OAUTH_PASSWORD", ""
).strip()
PUBLIC_ORIGIN = "https://public.example"

pytestmark = pytest.mark.skipif(
    not BASE_URL or not PASSWORD,
    reason="isolated OAuth staging service is not configured",
)


def test_dcr_pkce_refresh_and_protected_mcp_round_trip() -> None:
    callback = "https://client.example/callback"
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    resource = f"{PUBLIC_ORIGIN}/mcp"

    with httpx.Client(base_url=BASE_URL, timeout=30.0, trust_env=False) as client:
        authorization_metadata = client.get(
            "/.well-known/oauth-authorization-server"
        )
        protected_metadata = client.get(
            "/.well-known/oauth-protected-resource/mcp"
        )
        assert authorization_metadata.status_code == 200
        assert protected_metadata.status_code == 200
        assert protected_metadata.json()["resource"] == resource

        registration = client.post(
            "/oauth/register",
            json={
                "redirect_uris": [callback],
                "client_name": "Orion R1 Staging",
            },
        )
        assert registration.status_code == 201, registration.text
        client_id = registration.json()["client_id"]

        authorized = client.post(
            "/oauth/authorize",
            data={
                "password": PASSWORD,
                "client_id": client_id,
                "redirect_uri": callback,
                "state": "orion-r1-staging",
                "scope": "mcp",
                "resource": resource,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )
        assert authorized.status_code == 302, authorized.text
        code = parse_qs(
            urlsplit(authorized.headers["location"]).query
        )["code"][0]

        exchanged = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "client_id": client_id,
                "redirect_uri": callback,
                "resource": "https://PUBLIC.example:443/mcp/",
            },
        )
        assert exchanged.status_code == 200, exchanged.text
        access_token = exchanged.json()["access_token"]
        refresh_token = exchanged.json()["refresh_token"]

        initialized = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "orion-r1-staging",
                        "version": "1",
                    },
                },
            },
        )
        assert initialized.status_code == 200, initialized.text
        assert initialized.json()["result"]["protocolVersion"] == "2025-03-26"

        refreshed = client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "resource": resource,
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        refreshed_access = refreshed.json()["access_token"]
        assert refreshed_access
        assert refreshed.json()["refresh_token"]

        refresh_replay = client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "resource": resource,
            },
        )
        assert refresh_replay.status_code == 400
        assert refresh_replay.json()["error"] == "invalid_grant"

        tools = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {refreshed_access}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
        )
        assert tools.status_code == 200, tools.text
        assert len(tools.json()["result"]["tools"]) == 14
