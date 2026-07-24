"""OrionsBrain 私有 Nebula HTTP 合同的本地隔离测试。"""

from __future__ import annotations

import json

import pytest

import orionsbrain_ext.nebula.routes as nebula_routes
import web


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes[(method, path)] = handler
            return handler

        return decorator


class FakeRequest:
    def __init__(
        self,
        *,
        body=None,
        headers=None,
        query_params=None,
        path_params=None,
        json_error=None,
    ):
        self._body = {} if body is None else body
        self._json_error = json_error
        self.headers = {} if headers is None else headers
        self.query_params = {} if query_params is None else query_params
        self.path_params = {} if path_params is None else path_params

    async def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._body


class FakeDecayEngine:
    def calculate_score(self, metadata):
        return float(metadata.get("test_score", 0))


class FakeBucketManager:
    def __init__(self, buckets, anchors=None):
        self.buckets = list(buckets)
        self.anchors = list(anchors or [])
        self.calls = []

    async def list_all(self, *, include_archive=False):
        self.calls.append(("list_all", include_archive))
        return list(self.buckets)

    async def get(self, bucket_id):
        self.calls.append(("get", bucket_id))
        return next(
            (
                bucket
                for bucket in self.buckets + self.anchors
                if bucket["id"] == bucket_id
            ),
            None,
        )

    async def list_anchors(self):
        self.calls.append(("list_anchors",))
        return list(self.anchors)


def _payload(response):
    return json.loads(response.body.decode("utf-8"))


def _request(*, body=None, query_params=None, path_params=None):
    return FakeRequest(
        body=body,
        headers={"x-ombre-secret": " test-secret "},
        query_params=query_params,
        path_params=path_params,
    )


def _bucket(
    bucket_id,
    *,
    bucket_type="dynamic",
    content="正文",
    created="2026-07-24T01:00:00+08:00",
    **metadata,
):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "name": bucket_id,
            "type": bucket_type,
            "created": created,
            "last_active": created,
            "importance": 5,
            "valence": 0.5,
            "arousal": 0.3,
            **metadata,
        },
    }


@pytest.fixture
def registered(monkeypatch):
    monkeypatch.setenv("OMBRE_DESIRE_TOKEN", " test-secret ")
    mcp = FakeMCP()
    nebula_routes.register(mcp)
    return mcp


def test_extension_registers_exact_private_route_surface(registered):
    assert set(registered.routes) == {
        ("POST", "/api/desire/recall"),
        ("POST", "/api/desire/breath"),
        ("POST", "/api/desire/hold"),
        ("POST", "/api/desire/dream"),
        ("POST", "/api/desire/trace"),
        ("GET", "/api/desire/buckets"),
        ("GET", "/api/desire/bucket/{bucket_id}"),
        ("GET", "/api/desire/letters-list"),
        ("GET", "/api/desire/anchors-list"),
        ("GET", "/api/desire/i-notes"),
        ("GET", "/api/desire/brain-stats"),
    }
    assert [name for name, _register in web._WEB_MODULES].count(
        "orionsbrain_ext.nebula"
    ) == 1


def test_web_lazy_registration_reaches_extension_without_import_cycle():
    mcp = FakeMCP()

    web._register_orion_nebula(mcp)

    assert ("POST", "/api/desire/breath") in mcp.routes
    assert ("GET", "/api/desire/brain-stats") in mcp.routes


@pytest.mark.asyncio
async def test_auth_fails_closed_when_server_secret_is_missing(
    monkeypatch, registered
):
    monkeypatch.delenv("OMBRE_DESIRE_TOKEN", raising=False)
    response = await registered.routes[
        ("POST", "/api/desire/breath")
    ](FakeRequest(headers={"x-ombre-secret": "test-secret"}))

    assert response.status_code == 401
    assert _payload(response) == {"error": "Unauthorized"}


@pytest.mark.asyncio
async def test_breath_preserves_defaults_and_normalizes_bridge_tag_array(
    monkeypatch, registered
):
    received = {}

    async def fake_dispatch(**kwargs):
        received.update(kwargs)
        return "醒来文本"

    monkeypatch.setattr(nebula_routes._tool_breath, "dispatch", fake_dispatch)
    response = await registered.routes[
        ("POST", "/api/desire/breath")
    ](
        _request(
            body={
                "tags": ["relation", " home ", ""],
                "max_results": "7",
                "valence": "0.6",
            }
        )
    )

    assert response.status_code == 200
    assert _payload(response) == {"ok": True, "text": "醒来文本"}
    assert received == {
        "query": "",
        "max_tokens": 0,
        "domain": "",
        "valence": 0.6,
        "arousal": -1,
        "max_results": 7,
        "importance_min": -1,
        "tags": "relation,home",
    }


@pytest.mark.asyncio
async def test_hold_keeps_text_and_adds_structured_bucket_id(
    monkeypatch, registered
):
    received = {}

    async def fake_dispatch(**kwargs):
        received.update(kwargs)
        return "新建→memory-20260724 relation"

    monkeypatch.setattr(nebula_routes._tool_hold, "dispatch", fake_dispatch)
    response = await registered.routes[
        ("POST", "/api/desire/hold")
    ](
        _request(
            body={
                "content": "  一段记忆  ",
                "tags": ["relation", "home"],
                "importance": "7",
                "pinned": "false",
                "feel": False,
            }
        )
    )

    assert response.status_code == 200
    assert _payload(response) == {
        "ok": True,
        "text": "新建→memory-20260724 relation",
        "bucket_id": "memory-20260724",
    }
    assert received["content"] == "一段记忆"
    assert received["tags"] == "relation,home"
    assert received["importance"] == 7
    assert received["pinned"] is False
    assert received["feel"] is False
    assert "meaning" not in received
    assert "media" not in received
    assert "test_data" not in received


@pytest.mark.asyncio
async def test_trace_forces_non_delete_and_does_not_expose_new_dangerous_fields(
    monkeypatch, registered
):
    received = {}

    async def fake_dispatch(**kwargs):
        received.update(kwargs)
        return "已更新"

    monkeypatch.setattr(nebula_routes._tool_trace, "dispatch", fake_dispatch)
    response = await registered.routes[
        ("POST", "/api/desire/trace")
    ](
        _request(
            body={
                "bucket_id": "memory-1",
                "tags": ["current", "project"],
                "delete": True,
                "hard_delete": True,
                "restore": True,
                "old_str": "旧内容",
                "new_str": "新内容",
            }
        )
    )

    assert response.status_code == 200
    assert _payload(response) == {"ok": True, "text": "已更新"}
    assert received["bucket_id"] == "memory-1"
    assert received["tags"] == "current,project"
    assert received["delete"] is False
    assert "hard_delete" not in received
    assert "restore" not in received
    assert "old_str" not in received
    assert "new_str" not in received


@pytest.mark.asyncio
async def test_recall_keeps_positive_high_arousal_shape(
    monkeypatch, registered
):
    manager = FakeBucketManager(
        [
            _bucket(
                "warm",
                content="一起回家",
                arousal=0.95,
                valence=0.9,
                name="温暖",
            ),
            _bucket(
                "pain",
                content="高激动的痛",
                arousal=0.99,
                valence=0.1,
            ),
        ]
    )
    monkeypatch.setattr(nebula_routes.sh, "bucket_mgr", manager)

    response = await registered.routes[
        ("POST", "/api/desire/recall")
    ](_request(body={"query": "回家"}))

    assert response.status_code == 200
    payload = _payload(response)
    assert payload["ok"] is True
    assert payload["count"] == 1
    assert payload["memories"][0]["id"] == "warm"
    assert manager.calls == [("list_all", True)]


@pytest.mark.asyncio
async def test_six_dashboard_routes_keep_shapes_and_only_use_read_methods(
    monkeypatch, registered
):
    memory = _bucket(
        "memory",
        content="[[逐字]]正文",
        pinned=True,
        anchor=True,
        tags=["home"],
        why_remembered="有重量",
        test_score=9,
    )
    feel = _bucket("feel", bucket_type="feel", test_score=8)
    letter = _bucket(
        "letter",
        bucket_type="letter",
        content="一封信",
        author="Orion",
        title="标题",
        letter_date="2026-07-23",
    )
    note = _bucket(
        "note",
        bucket_type="i",
        tags=["aspect:边界"],
        content="自我认识",
    )
    deleted = _bucket(
        "deleted",
        content="不应出现",
        deleted_at="2026-07-20T00:00:00Z",
    )
    anchor = _bucket("anchor", content="地标", test_score=10)
    manager = FakeBucketManager(
        [memory, feel, letter, note, deleted], anchors=[anchor]
    )
    monkeypatch.setattr(nebula_routes.sh, "bucket_mgr", manager)
    monkeypatch.setattr(
        nebula_routes.sh, "decay_engine", FakeDecayEngine()
    )

    buckets_response = await registered.routes[
        ("GET", "/api/desire/buckets")
    ](_request(query_params={"limit": "not-an-int"}))
    bucket_response = await registered.routes[
        ("GET", "/api/desire/bucket/{bucket_id}")
    ](_request(path_params={"bucket_id": "memory"}))
    letters_response = await registered.routes[
        ("GET", "/api/desire/letters-list")
    ](_request())
    anchors_response = await registered.routes[
        ("GET", "/api/desire/anchors-list")
    ](_request())
    notes_response = await registered.routes[
        ("GET", "/api/desire/i-notes")
    ](_request())
    stats_response = await registered.routes[
        ("GET", "/api/desire/brain-stats")
    ](_request())

    assert _payload(buckets_response)["total"] == 2
    detail = _payload(bucket_response)
    assert detail["id"] == "memory"
    assert detail["content"] == "逐字正文"
    assert detail["tags"] == ["home"]
    assert _payload(letters_response) == {
        "total": 1,
        "letters": [
            {
                "id": "letter",
                "author": "Orion",
                "title": "标题",
                "date": "2026-07-23",
                "content": "一封信",
            }
        ],
    }
    assert _payload(anchors_response)["anchors"][0]["id"] == "anchor"
    assert _payload(notes_response)["notes"][0]["aspect"] == "边界"
    stats = _payload(stats_response)
    assert stats["total"] == 2
    assert stats["pinned"] == 1
    assert stats["feel"] == 1
    assert stats["letters"] == 1
    assert stats["anchors"] == 1
    assert set(manager.calls).issubset(
        {
            ("list_all", False),
            ("get", "memory"),
            ("list_anchors",),
        }
    )
