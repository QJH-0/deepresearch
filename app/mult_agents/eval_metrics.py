"""研报质量指标：从最终 State 与报告正文计算**规则型**指标。

与 `app/test/eval_metrics.py`（LLM-as-Judge 脚本）的分工：

- 本模块：纯函数、零外部依赖、可离线单测。用于**回归对比**——
  改一处代码后立刻能看出证据有没有变脏、检索有没有变浅、引用有没有失效。
- 脚本：需要真实模型与中间件，用 LLM-as-Judge 做语义级评估。

规则型指标会低估同义表述（如要点写作「状态机」而报告写「有向图」），
因此**不替代语义评估**，只作为「改动是否引入退化」的快速哨兵。

指标清单与各自的用途：

| 指标 | 回答的问题 |
| --- | --- |
| `evidence_duplication_rate` | State 里有没有重复证据堆积（reducer 语义是否正确） |
| `retrieval_rounds` | 检索深度是多少（自适应检索是否生效） |
| `citation_legality` | 正文引用角标是否都能在来源表里找到 |
| `key_point_coverage` | 报告字面覆盖了多少期望要点 |
| `expected_source_recall` | 期望来源是否真被引用（检索召回的底线，需人工标注） |

> ⚠️ `key_point_coverage` 只有要点本身有判别力时才有意义。实测教训：若要点写成
> 「框架名 + 通用概念」（LangGraph / 状态机 / 工具调用），连只跑 1 轮迭代的
> baseline 都能拿满分，指标触顶、测不出任何改进。要点须是「只有真正找到并读懂
> 正确来源才可能命中」的具体机制 / 版本 / 数字 / 对比结论。
"""

from .nodes._fallbacks import _extract_citation_ids

# 证据来源字段：web / local 两条链路的累积字段
_EVIDENCE_CHANNELS = ("web_evidence", "local_evidence", "evidence_pool")


def _source_ids(items) -> list[str]:
    return [
        str(item.get("source_id", "")).strip()
        for item in (items or [])
        if isinstance(item, dict) and str(item.get("source_id", "")).strip()
    ]


def evidence_duplication_rate(state: dict) -> dict:
    """各证据字段的重复率 = 1 - 去重后条数 / 总条数。

    这是 reducer 语义的哨兵：若节点返回了全量（而非增量），
    reducer 会再拼一次，重复率会随迭代轮次迅速升高。
    """
    per_channel = {}
    total_items = 0
    total_unique = 0
    for channel in _EVIDENCE_CHANNELS:
        ids = _source_ids(state.get(channel))
        unique = len(set(ids))
        per_channel[channel] = {
            "total": len(ids),
            "unique": unique,
            "duplication_rate": round(1 - unique / len(ids), 4) if ids else 0.0,
        }
        total_items += len(ids)
        total_unique += unique
    return {
        "overall": round(1 - total_unique / total_items, 4) if total_items else 0.0,
        "total_items": total_items,
        "unique_items": total_unique,
        "per_channel": per_channel,
    }


def retrieval_rounds(state: dict) -> dict:
    """检索轮次分布：外层研究轮数、每轮实际执行的检索查询数。

    `iteration` 记的是外层研究轮次（reflect 回环）；检索轨迹里每条对应一次查询。
    自适应检索（grader 驱动的内层重检）生效后，`queries_per_round` 会上升——
    这正是判断「检索是否真的变深了」的依据。
    """
    traces = [
        trace
        for trace in (list(state.get("web_search_trace") or []) + list(state.get("local_rag_trace") or []))
        if isinstance(trace, dict)
    ]
    by_round: dict[int, int] = {}
    for trace in traces:
        round_index = int(trace.get("iteration", 0))
        by_round[round_index] = by_round.get(round_index, 0) + 1

    outer_rounds = int(state.get("iteration", 0)) + 1
    return {
        "outer_rounds": outer_rounds,
        "total_queries": len(traces),
        "queries_per_round": round(len(traces) / outer_rounds, 2) if outer_rounds else 0.0,
        "by_round": {str(key): value for key, value in sorted(by_round.items())},
    }


def citation_legality(report: str, source_index) -> dict:
    """正文引用角标的合法性：能否在来源表里找到对应 source_id。

    只做规则校验（角标存在性），语义匹配由脚本的 LLM-as-Judge 负责。
    非法角标意味着引用溯源断链——报告导出前 `report_check` 会拦截，此处用于评测。
    """
    cited = _extract_citation_ids(report or "")
    valid_ids = set(_source_ids(source_index))
    legal = [item for item in cited if item in valid_ids]
    illegal = [item for item in cited if item not in valid_ids]
    return {
        "total": len(cited),
        "legal": len(legal),
        "illegal": len(illegal),
        "legality_rate": round(len(legal) / len(cited), 4) if cited else 0.0,
        "illegal_ids": illegal[:20],
    }


def key_point_coverage(report: str, key_points) -> dict:
    """期望要点的字面命中率（大小写不敏感的子串匹配）。

    会低估同义表述，定位是回归哨兵而非质量结论。
    """
    points = [str(point) for point in (key_points or []) if str(point).strip()]
    text = (report or "").lower()
    hit = [point for point in points if point.strip().lower() in text]
    missed = [point for point in points if point not in hit]
    return {
        "total": len(points),
        "hit": len(hit),
        "missed": missed,
        "coverage": round(len(hit) / len(points), 4) if points else 0.0,
    }


def aggregate(per_query: list[dict], key: str, field: str) -> float:
    """按字段求均值，跳过缺失与 None。

    `per_query` 为逐题指标列表，`key` 是指标名（如 `evidence_duplication_rate`），
    `field` 是该指标内的字段名（如 `overall`）。
    """
    values = []
    for entry in per_query or []:
        metric = (entry or {}).get(key)
        if not isinstance(metric, dict):
            continue
        value = metric.get(field)
        if isinstance(value, (int, float)):
            values.append(float(value))
    return round(sum(values) / len(values), 4) if values else 0.0


def expected_source_recall(report: str, source_index, expected_sources) -> dict:
    """期望来源的召回率：报告引用的来源里，命中期望域名/URL 片段的比例。

    `expected_sources` 留空时 `recall` 返回 **None**（而非 0.0）——聚合会跳过 None，
    避免把「这题没标注期望来源」误算成「一个都没召回」。
    """
    hints = [str(hint).strip().lower() for hint in (expected_sources or []) if str(hint).strip()]
    if not hints:
        return {"applicable": False, "total": 0, "hit": 0, "recall": None, "missed": []}

    cited_ids = _extract_citation_ids(report or "")
    lookup = {}
    for item in source_index or []:
        if not isinstance(item, dict):
            continue
        source_id = str(item.get("source_id", "")).strip()
        if source_id:
            lookup[source_id] = f"{item.get('locator', '')} {item.get('label', '')}".lower()

    cited_text = " ".join(lookup.get(source_id, "") for source_id in cited_ids)
    hit = [hint for hint in hints if hint in cited_text]
    return {
        "applicable": True,
        "total": len(hints),
        "hit": len(hit),
        "recall": round(len(hit) / len(hints), 4),
        "missed": [hint for hint in hints if hint not in hit],
    }


def measure(state: dict, report: str, key_points=None, expected_sources=None) -> dict:
    """一次算齐全部规则型指标。"""
    return {
        "evidence_duplication_rate": evidence_duplication_rate(state),
        "retrieval_rounds": retrieval_rounds(state),
        "citation_legality": citation_legality(report, state.get("source_index")),
        "key_point_coverage": key_point_coverage(report, key_points or []),
        "expected_source_recall": expected_source_recall(
            report, state.get("source_index"), expected_sources
        ),
    }
