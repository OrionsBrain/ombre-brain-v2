"""真实 HTTP 进程上的 Orion ↔ Nebula 私有合同验收。

只在显式提供隔离服务地址和测试 secret 时运行；普通单测默认跳过。
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest


BASE_URL = os.environ.get("OMBRE_DOCKER_WEB_BASE_URL", "").strip().rstrip("/")
DESIRE_TOKEN = os.environ.get("OMBRE_DESIRE_TOKEN", "").strip()

pytestmark = pytest.mark.skipif(
    not BASE_URL or not DESIRE_TOKEN,
    reason="OrionsBrain private HTTP staging service is not configured",
)


def test_private_desire_contract_round_trip_and_read_only_dashboard() -> None:
    marker = f"orion-r1-http-{uuid.uuid4().hex}"
    headers = {"X-Ombre-Secret": DESIRE_TOKEN}

    with httpx.Client(base_url=BASE_URL, timeout=30.0, trust_env=False) as client:
        unauthorized = client.post("/api/desire/breath", json={})
        assert unauthorized.status_code == 401
        assert unauthorized.json() == {"error": "Unauthorized"}

        held = client.post(
            "/api/desire/hold",
            headers=headers,
            json={
                "content": marker,
                "tags": ["r1", "bridge"],
                "importance": 7,
                "pinned": "false",
                "feel": False,
            },
        )
        assert held.status_code == 200, held.text
        held_payload = held.json()
        assert held_payload["ok"] is True
        assert held_payload["text"]
        bucket_id = held_payload["bucket_id"]
        assert bucket_id

        detail = client.get(
            f"/api/desire/bucket/{bucket_id}", headers=headers
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["id"] == bucket_id
        assert detail.json()["content"] == marker
        assert detail.json()["pinned"] is False

        directed = client.post(
            "/api/desire/breath",
            headers=headers,
            json={
                "query": marker,
                "tags": ["r1", "bridge"],
                "max_results": 5,
                "max_tokens": 6000,
                "importance_min": 1,
            },
        )
        assert directed.status_code == 200, directed.text
        assert directed.json()["ok"] is True
        assert marker in directed.json()["text"]
        assert bucket_id in directed.json()["text"]

        traced = client.post(
            "/api/desire/trace",
            headers=headers,
            json={
                "bucket_id": bucket_id,
                "importance": 8,
                "delete": True,
                "hard_delete": True,
                "restore": True,
                "old_str": marker,
                "new_str": "不应被私有入口采用",
            },
        )
        assert traced.status_code == 200, traced.text
        assert traced.json()["ok"] is True

        after_trace = client.get(
            f"/api/desire/bucket/{bucket_id}", headers=headers
        )
        assert after_trace.status_code == 200, after_trace.text
        after_payload = after_trace.json()
        assert after_payload["content"] == marker
        assert after_payload["importance"] == 8

        recalled = client.post(
            "/api/desire/recall",
            headers=headers,
            json={
                "query": marker,
                "arousal_min": 0,
                "valence_min": 0,
                "limit": 2,
            },
        )
        assert recalled.status_code == 200, recalled.text
        assert recalled.json()["ok"] is True
        assert bucket_id in {
            memory["id"] for memory in recalled.json()["memories"]
        }

        dreamed = client.post(
            "/api/desire/dream",
            headers=headers,
            json={"window_hours": 48},
        )
        assert dreamed.status_code == 200, dreamed.text
        assert dreamed.json()["ok"] is True
        assert dreamed.json()["text"]

        buckets = client.get("/api/desire/buckets", headers=headers)
        assert buckets.status_code == 200, buckets.text
        assert bucket_id in {
            bucket["id"] for bucket in buckets.json()["buckets"]
        }

        letters = client.get("/api/desire/letters-list", headers=headers)
        anchors = client.get("/api/desire/anchors-list", headers=headers)
        notes = client.get("/api/desire/i-notes", headers=headers)
        stats = client.get("/api/desire/brain-stats", headers=headers)

        assert set(letters.json()) == {"total", "letters"}
        assert set(anchors.json()) == {"anchors"}
        assert set(notes.json()) == {"notes"}
        assert set(stats.json()) == {
            "total",
            "pinned",
            "feel",
            "today_new",
            "letters",
            "anchors",
        }
