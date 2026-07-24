"""
========================================
tools/hold/feel.py — hold(feel=True) 分支
========================================

把模型自己的第一人称感受作为一条 feel 桶存下来。feel 桶是独立类型，
不参与普通 breath 浮现，只能通过 breath(domain="feel") 或 dream
末尾的 feel 段落读到。

关键行为：
- 写入时打上 __feel__ 系统标签 + domain=["feel"] + type="feel"
- valence/arousal 不传则取「我此刻的情绪」默认值（V0.5/A0.3）
- iter 2.0：bucket_id 用人类可读命名 ``feel_YYYYMMDDHHMM_V<valence*100>``
  （分钟精度 + valence 后缀），冲突时由 bucket_manager.create() 自动追加秒后缀
- iter 2.0：source_tool="hold"（feel 在 hold 工具的 feel=True 分支里）
- 如果带了 source_bucket，把源记忆标为 digested 并存入「我视角的 valence」
- embedding 由 create() 尝试同步生成；不可用时仍保留逐字原文，稍后可 backfill
- 返回回声状态：只复用本地已存向量；队列未完成时明确说“稍后观察”，
  绝不为了即时查重额外阻塞一次 provider 请求

不做什么（边界）：
- 不做合并：feel 是「同一件事的不同视角」，不该合
- 不做 importance 校准：feel 一律 importance=5

对外暴露：store_feel(content, extra_tags, valence, arousal, source_bucket,
                     why_remembered, meaning, media, receipt_out) → str
========================================
"""

from datetime import datetime

from .. import _runtime as rt

_FEEL_ECHO_THRESHOLD = 0.7


def _build_feel_id(valence: float) -> str:
    """构造 feel 桶的可读 id：``feel_YYYYMMDDHHMM_V085``。

    valence ∈ [0,1]，取两位整数（×100，四舍五入），保证字典序稳定可读。
    冲突回避交给 bucket_manager.create() 的 bucket_id_override 机制。
    """
    ts = datetime.now().strftime("%Y%m%d%H%M")
    v_int = max(0, min(100, round(float(valence) * 100)))
    return f"feel_{ts}_V{v_int:03d}"


def _feel_name(bucket: dict) -> str:
    metadata = bucket.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    value = metadata.get("name") or bucket.get("name") or bucket.get("id") or "旧 feel"
    return str(value).replace("\r", " ").replace("\n", " ").strip() or "旧 feel"


async def _build_feel_echo(bucket_id: str) -> tuple[str, dict]:
    """Inspect only locally stored vectors; never trigger a provider request."""
    engine = rt.embedding_engine
    if not engine or not getattr(engine, "enabled", False):
        return (
            "回声：向量未启用，暂不判断相似 feel；原文已安全保存。",
            {"status": "disabled", "similar_count": 0, "nearest": None},
        )

    outbox = getattr(rt.bucket_mgr, "embedding_outbox", None)
    if outbox is not None:
        try:
            if outbox.is_pending(bucket_id):
                return (
                    "回声：向量已进入后台队列；不阻塞保存，"
                    "结晶会在索引完成后继续由 dream 观察。",
                    {"status": "queued", "similar_count": 0, "nearest": None},
                )
        except Exception as exc:
            rt.logger.warning("feel echo outbox check failed for %s: %s", bucket_id, exc)

    try:
        current = await engine.get_embedding(bucket_id)
    except Exception as exc:
        rt.logger.warning("feel echo embedding read failed for %s: %s", bucket_id, exc)
        current = None
    if current is None:
        return (
            "回声：向量暂未完成；原文已保存，稍后由 dream 继续观察。",
            {"status": "pending", "similar_count": 0, "nearest": None},
        )

    try:
        feels = [
            bucket
            for bucket in await rt.bucket_mgr.list_all(include_archive=False)
            if bucket.get("id") != bucket_id
            and (bucket.get("metadata", {}) or {}).get("type") == "feel"
        ]
        scored: list[tuple[float, dict]] = []
        for bucket in feels:
            other_id = str(bucket.get("id") or "")
            if not other_id:
                continue
            other = await engine.get_embedding(other_id)
            if other is None:
                continue
            similarity = float(engine._cosine_similarity(current, other))
            scored.append((similarity, bucket))
        scored.sort(key=lambda item: item[0], reverse=True)
    except Exception as exc:
        rt.logger.warning("feel echo comparison failed for %s: %s", bucket_id, exc)
        return (
            "回声：本次相似 feel 比较未完成；原文已保存，不影响稍后的 dream 观察。",
            {"status": "comparison_failed", "similar_count": 0, "nearest": None},
        )

    if not scored:
        return (
            "回声：当前没有已索引的旧 feel 可比较，可以放心留下这次感受。",
            {"status": "indexed", "similar_count": 0, "nearest": None},
        )

    best_score, best_bucket = scored[0]
    similar_count = sum(
        1 for score, _bucket in scored if score > _FEEL_ECHO_THRESHOLD
    )
    nearest = {
        "bucket_id": str(best_bucket.get("id") or ""),
        "name": _feel_name(best_bucket),
        "score": round(best_score, 4),
        "score_kind": "cosine_0_1",
    }
    if similar_count:
        return (
            f"回声：与已有 {similar_count} 条 feel 相似"
            f"（最高「{nearest['name']}」{best_score:.2f}）。"
            "同一件事反复浮上来，可能正在成为确信；是否钉选仍由清醒时决定。",
            {"status": "indexed", "similar_count": similar_count, "nearest": nearest},
        )
    return (
        f"回声：最相近的旧 feel 是「{nearest['name']}」{best_score:.2f}，"
        f"未超过 {_FEEL_ECHO_THRESHOLD:.2f} 的结晶观察线。",
        {"status": "indexed", "similar_count": 0, "nearest": nearest},
    )


async def store_feel(
    content: str,
    extra_tags: list,
    valence: float,
    arousal: float,
    source_bucket: str,
    why_remembered: str,
    meaning: str = "",
    media: list | None = None,
    receipt_out: dict | None = None,
) -> str:
    feel_valence = valence if 0 <= valence <= 1 else 0.5
    feel_arousal = arousal if 0 <= arousal <= 1 else 0.3
    feel_tags = list(dict.fromkeys(["__feel__"] + extra_tags))
    bucket_id = await rt.bucket_mgr.create(
        content=content,
        tags=feel_tags,
        importance=5,
        domain=["feel"],
        valence=feel_valence,
        arousal=feel_arousal,
        name=None,
        bucket_type="feel",
        why_remembered=why_remembered,
        triggered_by=source_bucket.strip() if source_bucket else "",
        source_tool="hold",
        bucket_id_override=_build_feel_id(feel_valence),
        allow_embedding_fallback=True,
        meaning=meaning,
        media=media,
    )
    if source_bucket and source_bucket.strip():
        try:
            update_kwargs: dict[str, bool | float] = {"digested": True}
            if 0 <= valence <= 1:
                update_kwargs["model_valence"] = feel_valence
            await rt.bucket_mgr.update(source_bucket.strip(), **update_kwargs)
        except Exception as e:
            rt.logger.warning(f"Failed to mark source as digested / 标记已消化失败: {e}")
    echo_text, echo_receipt = await _build_feel_echo(bucket_id)
    if receipt_out is not None:
        receipt_out.update({
            "action": "feel",
            "reason": f"feel_{echo_receipt['status']}",
            "bucket_id": bucket_id,
            "echo": echo_receipt,
        })
    return f"🫧feel→{bucket_id}\n{echo_text}"
