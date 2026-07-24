"""
========================================
tools/hold/core.py — hold 普通存入分支（含自动合并）
========================================

非 feel、非 pinned 时走这里：优先调 LLM 自动打标，失败则用本地中性元数据，
再用检索找近似桶，
超过 merge_threshold 则合并（hold 用 raw_merge=True 拼接原文，不压缩），
否则新建。

关键行为：
- analyze() 失败（API key/限流/网络不可用）时仍逐字保存正文，只降级元数据
- 她/他显式 valence/arousal 优先于 LLM 打标
- 调 _common.merge_or_create 走合并/新建
- iter 2.0：source_tool 写 ``hold``；合并到老桶时只更新 ``last_merged_by``
- R1.1：返回落点证据（最近桶、邻近分、合并线、保护/同一事件决定原因），
  第一段仍保持 ``合并→ID`` / ``新建→ID`` 供旧客户端解析
- embedding 失败时桶正常创建，返回追加向量化降级警告
- 写完 fire-and-forget：plan 自动闭环判断 + 新桶疑似重复扫描

不做什么（边界）：
- 不做 pinned 配额检查（那是 pinned 分支的事）
- 不做单桶字节上限校验（已在 dispatch 入口做过）

对外暴露：store_core(content, extra_tags, importance, valence, arousal,
                     why_remembered, meaning, media) → str
========================================
"""

import asyncio

from .. import _runtime as rt
from .._common import merge_or_create, check_duplicate_for, check_plan_resolution


def _compact_number(value: float | int | None) -> str:
    if value is None:
        return ""
    return f"{float(value):.2f}".rstrip("0").rstrip(".")


def _safe_name(value: object, fallback: str = "旧桶") -> str:
    name = str(value or fallback).replace("\r", " ").replace("\n", " ").strip()
    return name or fallback


def _landing_evidence(outcome) -> str:
    """Turn an additive MergeOutcome into a short, honest Chinese receipt."""
    reason = str(getattr(outcome, "reason", "") or "")
    if not reason:
        return ""
    nearest_name = _safe_name(
        getattr(outcome, "nearest_name", ""),
        getattr(outcome, "nearest_id", "") or "旧桶",
    )
    score = _compact_number(getattr(outcome, "nearest_score", None))
    threshold = _compact_number(getattr(outcome, "merge_threshold", 75))
    protected_kind = str(getattr(outcome, "protected_kind", "") or "")
    protected_labels = {
        "pinned": "钉选记忆",
        "protected": "受保护记忆",
        "permanent": "永久记忆",
        "feel": "feel",
        "plan": "计划",
        "letter": "信件",
        "i": "自我认知",
        "terminal": "已归档终态记忆",
    }
    protected_label = protected_labels.get(
        protected_kind,
        protected_kind or "受保护记忆",
    )

    if reason == "merged_exact":
        return (
            f"落点：发现与「{nearest_name}」逐字相同的已有内容；"
            "重复正文已跳过，未经过 LLM 重写。"
        )
    if reason == "matched_exact_protected":
        return (
            f"落点：与{protected_label}「{nearest_name}」逐字相同；"
            "未修改受保护桶，直接返回已有落点。"
        )
    if reason == "merged_same_event":
        return (
            f"落点：与「{nearest_name}」邻近分 {score}/100，"
            f"超过合并线 {threshold}，且同一事件判定通过；已逐字追加。"
        )
    if reason == "created_below_threshold":
        return (
            f"落点：最邻近「{nearest_name}」{score}/100，"
            f"未超过合并线 {threshold}，因此新建。"
        )
    if reason == "created_no_candidate":
        return "落点：没有发现足够接近的旧桶，可以放心开荒。"
    if reason == "created_protected_target":
        return (
            f"落点：最邻近「{nearest_name}」{score}/100，但它是"
            f"{protected_label}；保护边界优先，因此新建。"
        )
    if reason == "created_separate_event":
        return (
            f"落点：与「{nearest_name}」邻近分 {score}/100，"
            f"虽超过合并线 {threshold}，但被判断为不同事件，因此保守新建。"
        )
    if reason in {"created_judge_unavailable", "created_judge_failed"}:
        return (
            f"落点：与「{nearest_name}」邻近分 {score}/100，"
            "但同一事件判定暂不可用，因此保守新建。"
        )
    if reason == "created_test_data":
        return "落点：测试数据按规则独立新建，不会自动并入正式记忆。"
    if reason == "created_search_failed":
        return "落点：合并检索暂不可用，已保守新建；正文仍已安全保存。"
    if reason in {
        "created_target_unavailable",
        "created_merge_update_failed",
        "created_merge_conflict",
        "created_merge_failed",
    }:
        return "落点：候选桶在提交时不可安全修改，已保守新建，未覆盖旧内容。"
    return ""


async def store_core(
    content: str,
    extra_tags: list,
    importance: int,
    valence: float,
    arousal: float,
    why_remembered: str,
    meaning: str = "",
    media: list | str | None = None,
    test_data: bool = False,
    receipt_out: dict | None = None,
) -> str:
    metadata_fallback = False
    try:
        analysis = await rt.dehydrator.analyze(content)
    except Exception as e:
        metadata_fallback = True
        rt.logger.warning(
            "hold metadata analysis failed; preserving raw content with local defaults / "
            f"hold 打标失败，使用本地默认元数据并原样保存正文: {type(e).__name__}: {e}"
        )
        default_analysis = getattr(rt.dehydrator, "_default_analysis", None)
        analysis = default_analysis() if callable(default_analysis) else {
            "domain": ["未分类"],
            "valence": 0.5,
            "arousal": 0.3,
            "tags": [],
            "suggested_name": "",
        }

    domain = analysis.get("domain") or ["未分类"]
    if not isinstance(domain, list):
        domain = ["未分类"]
    _v = analysis.get("valence", 0.5)
    _a = analysis.get("arousal", 0.3)
    final_valence = valence if 0 <= valence <= 1 else (float(_v) if _v is not None else 0.5)
    final_arousal = arousal if 0 <= arousal <= 1 else (float(_a) if _a is not None else 0.3)
    _raw_tags = analysis.get("tags") or []
    all_tags = list(dict.fromkeys((_raw_tags if isinstance(_raw_tags, list) else []) + extra_tags))
    suggested_name = analysis.get("suggested_name", "")

    outcome = await merge_or_create(
        content=content,
        tags=all_tags,
        importance=importance,
        domain=domain,
        valence=final_valence,
        arousal=final_arousal,
        name=suggested_name,
        raw_merge=True,
        why_remembered=why_remembered,
        source_tool="hold",
        meaning=meaning,
        media=media,
        test_data=test_data,
    )
    result_name, is_merged, embed_warn = outcome
    receipt_builder = getattr(outcome, "as_receipt", None)
    if receipt_out is not None and callable(receipt_builder):
        receipt_out.update(receipt_builder())

    action = "合并→" if is_merged else "新建→"
    asyncio.create_task(check_plan_resolution(content, source_bucket_id=result_name))
    if not is_merged:
        asyncio.create_task(check_duplicate_for(result_name, content))
    result = f"{action}{result_name} {','.join(str(d) for d in domain if d is not None)}"
    landing_evidence = _landing_evidence(outcome)
    if landing_evidence:
        result += f"\n{landing_evidence}"
    if embed_warn:
        result += f"\n⚠️ {embed_warn}"
    if metadata_fallback:
        result += "\n⚠️ 打标 API 暂不可用：正文已逐字保存，未做任何压缩；元数据暂用本地中性值。"
    return result
