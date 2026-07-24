from __future__ import annotations

from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from tools import _common as common
from tools import _runtime as rt
from tools.hold import core as hold_core
from tools.hold import feel as hold_feel


class _Manager:
    def __init__(
        self,
        candidate: dict | None = None,
        *,
        exact: bool = False,
    ):
        self.candidate = deepcopy(candidate) if candidate else None
        self.exact = exact
        self.embedding_outbox = None
        self.created: list[dict] = []
        self.updated: list[tuple[str, dict]] = []

    async def search(self, *_args, **_kwargs):
        return [deepcopy(self.candidate)] if self.candidate else []

    def find_exact_content(self, _content, domain_filter=None):
        assert domain_filter is None
        return deepcopy(self.candidate) if self.exact else None

    async def get(self, bucket_id):
        if self.candidate and self.candidate["id"] == bucket_id:
            return deepcopy(self.candidate)
        for bucket in self.created:
            if bucket["id"] == bucket_id:
                return deepcopy(bucket)
        return None

    async def update(self, bucket_id, **changes):
        assert self.candidate and self.candidate["id"] == bucket_id
        self.updated.append((bucket_id, deepcopy(changes)))
        if "content" in changes:
            self.candidate["content"] = changes["content"]
        metadata = self.candidate["metadata"]
        for key in (
            "tags",
            "importance",
            "domain",
            "valence",
            "arousal",
            "last_merged_by",
        ):
            if key in changes:
                metadata[key] = deepcopy(changes[key])
        return True

    async def create(self, **kwargs):
        bucket_id = f"new-{len(self.created) + 1}"
        bucket = {
            "id": bucket_id,
            "content": kwargs["content"],
            "metadata": {
                "id": bucket_id,
                "name": kwargs.get("name") or bucket_id,
                "type": "dynamic",
                "tags": list(kwargs.get("tags") or []),
                "domain": list(kwargs.get("domain") or []),
                "importance": kwargs.get("importance", 5),
                "valence": kwargs.get("valence", 0.5),
                "arousal": kwargs.get("arousal", 0.3),
            },
        }
        self.created.append(bucket)
        return bucket_id


class _Dehydrator:
    def __init__(self, *, same_event=True, confidence=0.95):
        self.same_event = same_event
        self.confidence = confidence
        self.merge_calls = 0

    async def analyze(self, _content):
        return {
            "domain": ["关系"],
            "valence": 0.7,
            "arousal": 0.4,
            "tags": ["共同生活"],
            "suggested_name": "新记忆",
        }

    async def judge_same_event(self, *_args, **_kwargs):
        return {
            "same_event": self.same_event,
            "confidence": self.confidence,
            "reason": "test judgement",
        }

    async def merge(self, *_args, **_kwargs):
        self.merge_calls += 1
        raise AssertionError("raw hold must never call LLM merge")

    def invalidate_cache(self, _content):
        return None


def _candidate(
    *,
    score=82,
    bucket_type="dynamic",
    pinned=False,
    protected=False,
    content="旧记忆原文",
):
    return {
        "id": "old-1",
        "content": content,
        "score": score,
        "metadata": {
            "id": "old-1",
            "name": "那次深夜谈话",
            "type": bucket_type,
            "pinned": pinned,
            "protected": protected,
            "tags": ["旧标签"],
            "domain": ["关系"],
            "importance": 6,
            "valence": 0.6,
            "arousal": 0.5,
        },
    }


def _install(monkeypatch, manager, dehydrator=None):
    monkeypatch.setattr(rt, "bucket_mgr", manager, raising=False)
    monkeypatch.setattr(rt, "embedding_engine", None, raising=False)
    monkeypatch.setattr(rt, "embedding_outbox", None, raising=False)
    monkeypatch.setattr(
        rt,
        "dehydrator",
        dehydrator or _Dehydrator(),
        raising=False,
    )
    monkeypatch.setattr(rt, "config", {"merge_threshold": 75}, raising=False)
    monkeypatch.setattr(rt, "logger", MagicMock(), raising=False)


async def _hold_outcome():
    return await common.merge_or_create(
        content="新记忆原文",
        tags=["新标签"],
        importance=5,
        domain=["关系"],
        valence=0.7,
        arousal=0.4,
        raw_merge=True,
        source_tool="hold",
    )


@pytest.mark.asyncio
async def test_high_score_same_event_returns_evidence_and_raw_appends(
    monkeypatch,
):
    manager = _Manager(_candidate(score=82))
    dehydrator = _Dehydrator(same_event=True, confidence=0.93)
    _install(monkeypatch, manager, dehydrator)

    outcome = await _hold_outcome()

    assert tuple(outcome) == ("old-1", True, "")
    assert len(outcome) == 3
    assert outcome[0] == "old-1"
    assert outcome.reason == "merged_same_event"
    assert outcome.nearest_name == "那次深夜谈话"
    assert outcome.nearest_score == 82
    assert outcome.same_event_confidence == 0.93
    assert manager.candidate["content"] == "旧记忆原文\n\n---\n新记忆原文"
    assert dehydrator.merge_calls == 0
    assert outcome.as_receipt()["nearest"]["score_kind"] == "retrieval_0_100"


@pytest.mark.asyncio
async def test_below_threshold_and_no_candidate_have_distinct_reasons(
    monkeypatch,
):
    manager = _Manager(_candidate(score=67))
    _install(monkeypatch, manager)
    below = await _hold_outcome()

    assert below.merged is False
    assert below.reason == "created_below_threshold"
    assert below.nearest_score == 67

    empty = _Manager()
    _install(monkeypatch, empty)
    no_candidate = await _hold_outcome()

    assert no_candidate.merged is False
    assert no_candidate.reason == "created_no_candidate"
    assert no_candidate.nearest_id == ""


@pytest.mark.asyncio
async def test_high_score_different_event_is_conservatively_created(
    monkeypatch,
):
    manager = _Manager(_candidate(score=84))
    _install(
        monkeypatch,
        manager,
        _Dehydrator(same_event=False, confidence=0.99),
    )

    outcome = await _hold_outcome()

    assert outcome.merged is False
    assert outcome.reason == "created_separate_event"
    assert outcome.same_event_confidence == 0.99
    assert manager.candidate["content"] == "旧记忆原文"
    assert manager.created[0]["content"] == "新记忆原文"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candidate", "kind"),
    [
        (_candidate(bucket_type="permanent"), "permanent"),
        (_candidate(pinned=True), "pinned"),
        (_candidate(protected=True), "protected"),
        (_candidate(bucket_type="feel"), "feel"),
        (_candidate(bucket_type="plan"), "plan"),
        (_candidate(bucket_type="letter"), "letter"),
        (_candidate(bucket_type="i"), "i"),
    ],
)
async def test_non_dynamic_targets_are_never_auto_merged(
    monkeypatch,
    candidate,
    kind,
):
    manager = _Manager(candidate)
    _install(monkeypatch, manager)

    outcome = await _hold_outcome()

    assert outcome.merged is False
    assert outcome.reason == "created_protected_target"
    assert outcome.protected_kind == kind
    assert manager.updated == []
    assert manager.candidate["content"] == "旧记忆原文"
    assert manager.created[0]["content"] == "新记忆原文"


@pytest.mark.asyncio
async def test_exact_duplicate_of_permanent_reuses_without_modifying(
    monkeypatch,
):
    candidate = _candidate(
        bucket_type="permanent",
        content="新记忆原文",
    )
    manager = _Manager(candidate, exact=True)
    _install(monkeypatch, manager)

    outcome = await _hold_outcome()

    assert tuple(outcome) == ("old-1", True, "")
    assert outcome.action == "existing"
    assert outcome.reason == "matched_exact_protected"
    assert outcome.protected_kind == "permanent"
    assert outcome.content_changed is False
    assert manager.updated == []
    assert manager.created == []


@pytest.mark.asyncio
async def test_store_core_keeps_legacy_prefix_and_adds_visible_receipt(
    monkeypatch,
):
    manager = _Manager(_candidate(score=67))
    _install(monkeypatch, manager)
    monkeypatch.setattr(
        hold_core,
        "check_plan_resolution",
        MagicMock(return_value=None),
    )
    monkeypatch.setattr(
        hold_core,
        "check_duplicate_for",
        MagicMock(return_value=None),
    )
    monkeypatch.setattr(
        hold_core.asyncio,
        "create_task",
        lambda _value: None,
    )
    receipt = {}

    text = await hold_core.store_core(
        "新记忆原文",
        extra_tags=[],
        importance=5,
        valence=-1,
        arousal=-1,
        why_remembered="",
        receipt_out=receipt,
    )

    assert text.startswith("新建→new-1 关系\n")
    assert "最邻近「那次深夜谈话」67/100" in text
    assert "未超过合并线 75" in text
    assert receipt["action"] == "created"
    assert receipt["reason"] == "created_below_threshold"
    assert receipt["bucket_id"] == "new-1"


class _FeelManager:
    embedding_outbox = None

    def __init__(self, buckets=None):
        self.updated = []
        self.buckets = list(buckets or [])

    async def create(self, **_kwargs):
        return "feel-new"

    async def update(self, bucket_id, **changes):
        self.updated.append((bucket_id, changes))
        return True

    async def list_all(self, include_archive=False):
        assert include_archive is False
        return list(self.buckets)


@pytest.mark.asyncio
async def test_feel_receipt_never_waits_for_disabled_embedding(monkeypatch):
    manager = _FeelManager()
    monkeypatch.setattr(hold_feel.rt, "bucket_mgr", manager, raising=False)
    monkeypatch.setattr(hold_feel.rt, "embedding_engine", None, raising=False)
    monkeypatch.setattr(hold_feel.rt, "logger", MagicMock(), raising=False)
    receipt = {}

    text = await hold_feel.store_feel(
        content="同一件事又浮上来了",
        extra_tags=[],
        valence=0.5,
        arousal=0.3,
        source_bucket="source-1",
        why_remembered="",
        receipt_out=receipt,
    )

    assert text.startswith("🫧feel→feel-new\n")
    assert "向量未启用" in text
    assert receipt == {
        "action": "feel",
        "reason": "feel_disabled",
        "bucket_id": "feel-new",
        "echo": {
            "status": "disabled",
            "similar_count": 0,
            "nearest": None,
        },
    }
    assert manager.updated == [
        ("source-1", {"digested": True, "model_valence": 0.5})
    ]


class _LocalFeelEngine:
    enabled = True

    def __init__(self):
        self.vectors = {
            "feel-new": [1.0, 0.0],
            "feel-old": [0.98, 0.02],
        }

    async def get_embedding(self, bucket_id):
        return self.vectors.get(bucket_id)

    @staticmethod
    def _cosine_similarity(left, right):
        numerator = sum(a * b for a, b in zip(left, right))
        left_norm = sum(value * value for value in left) ** 0.5
        right_norm = sum(value * value for value in right) ** 0.5
        return numerator / (left_norm * right_norm)

    async def search_similar(self, *_args, **_kwargs):
        raise AssertionError("feel receipt must not issue a provider-backed query")

    async def generate_and_store(self, *_args, **_kwargs):
        raise AssertionError("feel receipt must not generate another vector")


@pytest.mark.asyncio
async def test_feel_receipt_reuses_only_already_stored_vectors(monkeypatch):
    manager = _FeelManager([
        {
            "id": "feel-old",
            "content": "旧感受",
            "metadata": {"type": "feel", "name": "反复浮现的主题"},
        }
    ])
    engine = _LocalFeelEngine()
    monkeypatch.setattr(hold_feel.rt, "bucket_mgr", manager, raising=False)
    monkeypatch.setattr(hold_feel.rt, "embedding_engine", engine, raising=False)
    monkeypatch.setattr(hold_feel.rt, "logger", MagicMock(), raising=False)
    receipt = {}

    text = await hold_feel.store_feel(
        content="同一件事又浮上来了",
        extra_tags=[],
        valence=0.5,
        arousal=0.3,
        source_bucket="source-1",
        why_remembered="",
        receipt_out=receipt,
    )

    assert "与已有 1 条 feel 相似" in text
    assert "最高「反复浮现的主题」1.00" in text
    assert receipt["reason"] == "feel_indexed"
    assert receipt["echo"]["similar_count"] == 1
    assert receipt["echo"]["nearest"] == {
        "bucket_id": "feel-old",
        "name": "反复浮现的主题",
        "score": pytest.approx(0.9998),
        "score_kind": "cosine_0_1",
    }
