"""节点 prompt 的证据预算控制。

证据在 State 中跨轮累积，节点若每轮把全量证据塞进 prompt，token 数随迭代线性增长，
而模型在长上下文上的召回率会随之下降（context rot）。本模块是统一的
「按预算取证据」入口，避免各节点各写一套截断逻辑。

**阈值语义：防病态膨胀，不是正常路径裁剪。** 默认值刻意放宽，只在某轮检索异常
召回（数百条）时才生效。在建立检索质量评估集之前，收紧阈值无法验证是否伤及
报告质量，因此不把「省 token」当作本模块的目标。
"""

import json
import logging

logger = logging.getLogger("mult_agents")

DEFAULT_EVIDENCE_LIMIT = 40
DEFAULT_EVIDENCE_BUDGET_CHARS = 60000
SNIPPET_MAX_CHARS = 600

_TRUNCATABLE_FIELDS = ("snippet", "notes", "reliability_reason")


def _budget() -> tuple[int, int]:
    """读取 (条数上限, 字符预算)；配置不可用时回落模块默认值并留痕。"""
    try:
        from backend.config.settings import get_business_settings

        biz = get_business_settings()
    except Exception as exc:
        logger.warning("[context] 业务配置不可用，证据预算回落默认值 | %s", exc)
        return DEFAULT_EVIDENCE_LIMIT, DEFAULT_EVIDENCE_BUDGET_CHARS
    limit = getattr(biz, "context_evidence_limit", DEFAULT_EVIDENCE_LIMIT)
    budget = getattr(biz, "context_evidence_budget_chars", DEFAULT_EVIDENCE_BUDGET_CHARS)
    return int(limit), int(budget)


def _score(item: dict) -> float:
    """排序依据：可靠度优先（deep_dive 审计结果），其次相关度（检索期打分）。"""
    for key in ("reliability_score", "relevance_score"):
        value = item.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def _size(item: dict) -> int:
    return len(json.dumps(item, ensure_ascii=False))


def select_evidence(
    items: list,
    *,
    limit: int | None = None,
    budget_chars: int | None = None,
) -> list[dict]:
    """按分数取 top-k，并受字符预算约束。

    返回顺序保持**原始相对顺序**而非分数顺序：prompt 里证据的排列影响引用编号的
    可读性，重排会让引用角标与正文叙述脱节。

    装不下的大项直接跳过，让位给能装下的小项；若一条都装不下，仍保留分数最高的
    那一条——返回空列表会让下游误判为「没有检索到证据」。
    """
    valid = [item for item in items if isinstance(item, dict)]
    if not valid:
        return []
    if limit is None or budget_chars is None:
        default_limit, default_budget = _budget()
        limit = default_limit if limit is None else limit
        budget_chars = default_budget if budget_chars is None else budget_chars
    if len(valid) <= limit and sum(_size(item) for item in valid) <= budget_chars:
        return valid

    ranked = sorted(range(len(valid)), key=lambda index: _score(valid[index]), reverse=True)
    kept: set[int] = set()
    used = 0
    for index in ranked:
        if len(kept) >= limit:
            break
        size = _size(valid[index])
        if used + size > budget_chars:
            continue
        kept.add(index)
        used += size
    if not kept:
        kept.add(ranked[0])
    return [item for index, item in enumerate(valid) if index in kept]


def compact_evidence(items: list, *, snippet_max_chars: int = SNIPPET_MAX_CHARS) -> list[dict]:
    """截断长文本字段，保留全部结构化字段。

    只截断不丢字段：下游（analyze / write）依赖 source_id 与 snippet 做论断归属，
    丢字段会破坏引用溯源。返回新对象，不就地修改 State 中的证据。
    """
    compacted = []
    for item in items:
        if not isinstance(item, dict):
            continue
        trimmed = dict(item)
        for key in _TRUNCATABLE_FIELDS:
            text = trimmed.get(key)
            if isinstance(text, str) and len(text) > snippet_max_chars:
                trimmed[key] = text[:snippet_max_chars] + "…"
        compacted.append(trimmed)
    return compacted


def evidence_for_prompt(
    state: dict,
    *,
    limit: int | None = None,
    budget_chars: int | None = None,
) -> list[dict]:
    """节点入口：从 State 取证据 → 按预算裁剪 → 压缩长文本。

    优先用 `evidence_pool`（deep_dive 审计后的结果）；为空时回落到原始证据
    （deep_dive 之前的轮次还没有 pool），避免节点空手进 prompt。
    """
    items = state.get("evidence_pool") or (
        list(state.get("web_evidence", [])) + list(state.get("local_evidence", []))
    )
    selected = select_evidence(items, limit=limit, budget_chars=budget_chars)
    return compact_evidence(selected)


def raw_evidence_for_prompt(
    state: dict,
    source: str,
    *,
    limit: int | None = None,
    budget_chars: int | None = None,
) -> list[dict]:
    """裁判节点入口：取指定来源的**原始**证据并按预算裁剪。

    与 evidence_for_prompt 的区别：deep_dive 必须看到未经审计的原始证据，
    不能用 evidence_pool——那正是它自己的输出。
    超预算被裁掉的证据不会从证据池消失：deep_dive 会用确定性回填补齐，
    只是这些证据拿到的是先验分而非 LLM 裁判分。
    """
    items = list(state.get(source, []) or [])
    return compact_evidence(select_evidence(items, limit=limit, budget_chars=budget_chars))


CLAIM_PREVIEW_CHARS = 160

def build_research_note(state: dict, findings: list, missing_gaps: list, audit_flags: list) -> dict:
    """汇编本轮研究笔记：已确认结论 / 未解决缺口 / 低可信来源。

    刻意用确定性汇编而非再调一次 LLM：输入已是结构化结论，让模型转述一遍
    只增加成本与不确定性，不增加信息。
    """
    return {
        "iteration": int(state.get("iteration", 0)),
        "confirmed": [
            str(item.get("claim", ""))[:CLAIM_PREVIEW_CHARS]
            for item in findings
            if isinstance(item, dict) and item.get("claim")
        ],
        "open_questions": [str(gap) for gap in missing_gaps if str(gap).strip()],
        "low_confidence_sources": [
            str(flag.get("target", ""))
            for flag in audit_flags
            if isinstance(flag, dict) and flag.get("type") == "low_confidence" and flag.get("target")
        ],
    }


def render_research_notes(notes: list) -> str:
    """把历史研究笔记渲染成紧凑文本，供补搜规划节点避免重复检索。"""
    lines = []
    for note in notes or []:
        if not isinstance(note, dict):
            continue
        lines.append(f"[第 {int(note.get('iteration', 0)) + 1} 轮]")
        confirmed = note.get("confirmed") or []
        if confirmed:
            lines.append("已确认：" + "；".join(str(item) for item in confirmed))
        open_questions = note.get("open_questions") or []
        if open_questions:
            lines.append("未解决：" + "；".join(str(item) for item in open_questions))
        low_confidence = note.get("low_confidence_sources") or []
        if low_confidence:
            lines.append("低可信来源（勿再作为主要依据）：" + "、".join(str(item) for item in low_confidence))
    return "\n".join(lines)
