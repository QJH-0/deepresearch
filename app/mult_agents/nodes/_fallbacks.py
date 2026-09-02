"""nodes 包：拆分自 nodes.py（P1-2）。

纪律：纯搬迁，零行为变更。每个文件只负责一类节点或辅助函数。
"""
import json
import logging
import re

from ..state import AgentState

logger = logging.getLogger("mult_agents")


def _fallback_audit(state: AgentState) -> dict:
    evidence_pool = []
    source_index = []
    audit_flags = []
    for record in state.get("web_evidence", []) + state.get("local_evidence", []):
        score, reason = _score_evidence(record)
        normalized = dict(record)
        normalized["reliability_score"] = score
        normalized["reliability_reason"] = reason
        normalized["source_label"] = record.get("title") or record.get("doc_id") or record.get("url") or record.get("source_id")
        normalized.setdefault("supports", [])
        normalized.setdefault("refutes", [])
        evidence_pool.append(normalized)
        locator = record.get("url") or record.get("doc_id") or ""
        if score < 0.6:
            audit_flags.append({"type": "low_confidence", "target": record.get("source_id"), "reason": reason})
        else:
            source_index.append(
                {
                    "source_id": record.get("source_id"),
                    "label": normalized["source_label"],
                    "locator": locator or "未提供定位信息",
                    "source_type": record.get("source_type", "source"),
                }
            )
    return {
        "summary": "完成证据评分与审计。",
        "evidence_pool": evidence_pool,
        "audit_flags": audit_flags,
        "source_index": _dedupe_sources(source_index, ["source_id"]),
    }



def _fallback_analysis(state: AgentState) -> dict:
    source_ids = [item.get("source_id") for item in state.get("evidence_pool", [])[:3] if item.get("source_id")]
    findings = [
        {
            "claim_id": "c_1",
            "claim": f"围绕“{state['query']}”已完成多源检索，初步证据表明问题可以从网络与本地知识库双侧支撑。",
            "confidence": "medium" if source_ids else "low",
            "source_ids": source_ids,
        }
    ]
    return {
        "analysis_summary": "完成结论归纳与假设状态整理。",
        "findings": findings,
        "claim_map": [{"claim_id": item["claim_id"], "source_ids": item["source_ids"]} for item in findings],
        "next_actions": [] if source_ids else ["补充更多高质量来源"],
    }



def _render_fallback_report(state: AgentState) -> str:
    lines = ["# 调研结果", "", "## 执行摘要", state.get("analysis", "暂无分析结果"), ""]
    lines.append("")
    lines.append("## 核心结论")
    for finding in state.get("findings", []):
        refs = "".join(f"[{source_id}]" for source_id in finding.get("source_ids", []))
        lines.append(f"- {finding.get('claim', '')} {refs}".rstrip())
    lines.append("")
    lines.append("## 风险与不确定性")
    if state.get("audit_flags"):
        for flag in state["audit_flags"]:
            lines.append(f"- {flag.get('type')}: {flag.get('reason')} ({flag.get('target')})")
    else:
        lines.append("- 当前未发现明显冲突。")
    lines.append("")
    lines.append("## 检索统计")
    web_stats = state.get("web_retrieval_stats", {})
    local_stats = state.get("local_retrieval_stats", {})
    if web_stats or local_stats:
        lines.append(f"- 网络检索：queries={web_stats.get('query_count', 0)} raw={web_stats.get('raw_count', 0)} kept={web_stats.get('kept_count', 0)} dropped={web_stats.get('dropped_count', 0)}")
        lines.append(f"- 本地检索：queries={local_stats.get('query_count', 0)} raw={local_stats.get('raw_count', 0)} kept={local_stats.get('kept_count', 0)} dropped={local_stats.get('dropped_count', 0)}")
    else:
        lines.append("- 未记录检索统计。")
    lines.append("")
    lines.append("## 引用列表")
    for source in state.get("source_index", []):
        source_type = source.get("source_type", "source")
        lines.append(f"- {source.get('source_id')} [{source_type}]: {source.get('label')} | {source.get('locator')}")
    return "\n".join(lines)



def _build_source_lookup(state: AgentState) -> dict[str, dict]:
    lookup: dict[str, dict] = {}

    def _put(source_id: str, source_type: str, label: str, locator: str):
        if not source_id:
            return
        item = lookup.get(source_id)
        if not item:
            lookup[source_id] = {
                "source_id": source_id,
                "source_type": source_type or "source",
                "label": label or source_id,
                "locator": locator or "",
            }
            return
        if (not item.get("locator")) and locator:
            item["locator"] = locator
        if (not item.get("label")) and label:
            item["label"] = label
        if item.get("source_type") in {"source", ""} and source_type:
            item["source_type"] = source_type

    for source in state.get("source_index", []):
        _put(
            str(source.get("source_id", "")).strip(),
            str(source.get("source_type", "source")).strip(),
            str(source.get("label", "")).strip(),
            str(source.get("locator", "")).strip(),
        )
    for ev in state.get("evidence_pool", []):
        _put(
            str(ev.get("source_id", "")).strip(),
            str(ev.get("source_type", "source")).strip(),
            str(ev.get("title") or ev.get("source_label") or "").strip(),
            str(ev.get("url") or ev.get("doc_id") or "").strip(),
        )
    for ev in state.get("web_evidence", []):
        _put(
            str(ev.get("source_id", "")).strip(),
            "web",
            str(ev.get("title", "")).strip(),
            str(ev.get("url") or "").strip(),
        )
    for ev in state.get("local_evidence", []):
        _put(
            str(ev.get("source_id", "")).strip(),
            "local",
            str(ev.get("title") or ev.get("doc_id") or "").strip(),
            str(ev.get("doc_id") or "").strip(),
        )
    for key, item in lookup.items():
        if key.startswith("LOC"):
            item["source_type"] = "local"
        elif key.startswith("WEB"):
            item["source_type"] = "web"
    return lookup



def _extract_citation_ids(content: str) -> list[str]:
    """从正文中提取所有引用ID [XXX]"""
    pattern = r'\[([A-Z]+\d+_\d+-\d+)\]'
    matches = re.findall(pattern, content)
    return list(dict.fromkeys(matches))  # 去重保序



def _validate_and_fix_citations(content: str, valid_source_ids: set[str]) -> tuple[str, list[str]]:
    """校验正文中的引用ID，移除非法引用，返回修正后的内容和实际使用的合法引用列表"""
    pattern = r'\[([A-Z]+\d+_\d+-\d+)\]'
    
    def replace_citation(match):
        citation_id = match.group(1)
        if citation_id in valid_source_ids:
            return f"[{citation_id}]"
        else:
            # 非法引用，直接移除
            return ""
    
    fixed_content = re.sub(pattern, replace_citation, content)
    # 提取修正后实际使用的合法引用
    used_ids = [cid for cid in _extract_citation_ids(fixed_content) if cid in valid_source_ids]
    return fixed_content, used_ids



def _render_reference_list(state: AgentState) -> str:
    lines = ["## 参考资料"]
    lookup = _build_source_lookup(state)
    
    # 1. 优先从正文 draft 中按出现顺序提取实际引用的 source_id
    draft_content = state.get("draft", "") or state.get("final", "")
    cited_ids: list[str] = []
    if draft_content:
        for sid in _extract_citation_ids(draft_content):
            if sid in lookup and sid not in cited_ids:
                cited_ids.append(sid)
    
    # 2. 如果正文无引用，降级到 findings
    if not cited_ids:
        for finding in state.get("findings", []):
            for sid in finding.get("source_ids", []):
                text = str(sid).strip()
                if text and text not in cited_ids and text in lookup:
                    cited_ids.append(text)
    
    # 3. 再降级：全量 lookup
    if not cited_ids:
        cited_ids = list(lookup.keys())
    
    # 4. 对 local 来源按 locator 去重展示（同一文件多个 chunk 只展示一次）
    seen_locators: set[str] = set()
    display_ids: list[str] = []
    web_ids: list[str] = []
    local_ids: list[str] = []
    
    for sid in cited_ids:
        source = lookup.get(sid)
        if not source:
            continue
        source_type = source.get("source_type", "")
        locator = source.get("locator", "").strip()
        
        if source_type == "local":
            # 同一文件路径只保留第一次出现的 source_id 做代表
            dedup_key = locator or sid
            if dedup_key in seen_locators:
                continue
            seen_locators.add(dedup_key)
            local_ids.append(sid)
        else:
            web_ids.append(sid)
    
    # 5. 排列顺序：WEB 在前（保持原始引用顺序），LOCAL 跟后
    display_ids = web_ids + local_ids
    
    if not display_ids:
        display_ids = cited_ids[:15]
    
    for sid in display_ids:
        source = lookup.get(sid)
        if not source:
            continue
        locator = source.get("locator", "").strip()
        label = source.get("label", "").strip()
        source_type = source.get("source_type", "source")
        source_id = source.get("source_id", sid)
        
        if not locator:
            locator = "链接暂不可用" if source_type == "web" else "本地知识库"
        
        lines.append(f"- [{source_id}] [{source_type}]: {label} | {locator}")
    
    if len(lines) == 1:
        lines.append("- 暂无参考资料")
    return "\n".join(lines)



def _render_execution_appendix(state: AgentState) -> str:
    lines = ["## 规划与检索明细", "", "### 执行概览"]
    search_plan = state.get("search_plan", [])
    web_stats = state.get("web_retrieval_stats", {})
    local_stats = state.get("local_retrieval_stats", {})
    lines.append(f"- 规划生成研究问题数: {len(state.get('research_questions', []))}")
    lines.append(f"- 规划生成搜索步骤数: {len(search_plan)}")
    
    iteration = state.get("iteration", 0)
    lines.append(f"- 经过 {iteration + 1} 轮检索迭代")
    if state.get("needs_more_research"):
        lines.append(f"- 信息缺口: {state.get('missing_gaps', [])}")
        
    lines.append(
        f"- 实际执行网页检索问题数: {web_stats.get('query_count', 0)} | 原始命中: {web_stats.get('raw_count', 0)} | 保留证据: {web_stats.get('kept_count', 0)} | 丢弃: {web_stats.get('dropped_count', 0)}"
    )
    lines.append(
        f"- 实际执行本地检索问题数: {local_stats.get('query_count', 0)} | 原始命中: {local_stats.get('raw_count', 0)} | 保留证据: {local_stats.get('kept_count', 0)} | 丢弃: {local_stats.get('dropped_count', 0)}"
    )
    lines.append("")
    lines.append("### 问题拆解明细")
    for sq in state.get("sub_questions", []):
        lines.append(f"- {sq}")
    if not state.get("sub_questions"):
        lines.append("- 无")
    lines.append("")
    lines.append("### 规划输出")
    outline = state.get("outline", [])
    if outline:
        for section in outline:
            lines.append(
                f"- {section.get('id')}: {section.get('title')} | {section.get('description')} | search_queries={section.get('search_queries', [])}"
            )
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("### 研究问题")
    for index, question in enumerate(state.get("research_questions", []), 1):
        lines.append(f"- Q{index}: {question}")
    if not state.get("research_questions"):
        lines.append("- 无")
    lines.append("")
    lines.append("### 搜索计划")
    for index, item in enumerate(state.get("search_plan", []), 1):
        lines.append(
            f"- S{index}: section={item.get('section_id')} | query={item.get('query')} | source={item.get('source_preference')} | reason={item.get('reason')}"
        )
    if not state.get("search_plan"):
        lines.append("- 无")
    lines.append("")
    if state.get("supplementary_queries"):
        lines.append("### 补搜计划")
        for index, item in enumerate(state.get("supplementary_queries", []), 1):
            lines.append(f"- S{index} (补搜): query={item.get('query')} | reason={item.get('reason')}")
        lines.append("")
    lines.append("### 网页检索明细")
    for index, trace in enumerate(state.get("web_search_trace", []), 1):
        lines.append(
            f"- WQ{index}: section={trace.get('section_id')} | query={trace.get('query')} | reason={trace.get('reason')} | raw={trace.get('raw_count', 0)} | kept={trace.get('kept_count', 0)} | rejected={trace.get('rejected_count', 0)}"
        )
        lines.append(f"  - raw_ids={trace.get('raw_source_ids', [])}")
        lines.append(f"  - kept_ids={trace.get('kept_source_ids', [])}")
        lines.append(f"  - rejected_ids={trace.get('rejected_source_ids', [])}")
        if trace.get("reject_reason"):
            lines.append(f"  - reject_reason={trace.get('reject_reason')}")
        lines.append("  - raw_samples:")
        for item in trace.get("raw_records", [])[:3]:
            lines.append(f"    - {item.get('source_id')}: {item.get('title')} | {item.get('locator')}")
        if trace.get("kept_records"):
            lines.append("  - kept_samples:")
            for item in trace.get("kept_records", [])[:3]:
                lines.append(f"    - {item.get('source_id')}: {item.get('title')} | {item.get('locator')}")
        if trace.get("rejected_records"):
            lines.append("  - rejected_samples:")
            for item in trace.get("rejected_records", [])[:3]:
                lines.append(f"    - {item.get('source_id')}: {item.get('title')} | {item.get('locator')}")
    if not state.get("web_search_trace"):
        lines.append("- 无")
    lines.append("")
    lines.append("### 本地检索明细")
    for index, trace in enumerate(state.get("local_rag_trace", []), 1):
        lines.append(
            f"- LQ{index}: section={trace.get('section_id')} | query={trace.get('query')} | reason={trace.get('reason')} | raw={trace.get('raw_count', 0)} | kept={trace.get('kept_count', 0)} | rejected={trace.get('rejected_count', 0)}"
        )
        lines.append(f"  - raw_ids={trace.get('raw_source_ids', [])}")
        lines.append(f"  - kept_ids={trace.get('kept_source_ids', [])}")
        lines.append(f"  - rejected_ids={trace.get('rejected_source_ids', [])}")
        if trace.get("reject_reason"):
            lines.append(f"  - reject_reason={trace.get('reject_reason')}")
        lines.append("  - raw_samples:")
        for item in trace.get("raw_records", [])[:3]:
            lines.append(f"    - {item.get('source_id')}: {item.get('title')} | {item.get('locator')}")
        if trace.get("kept_records"):
            lines.append("  - kept_samples:")
            for item in trace.get("kept_records", [])[:3]:
                lines.append(f"    - {item.get('source_id')}: {item.get('title')} | {item.get('locator')}")
        if trace.get("rejected_records"):
            lines.append("  - rejected_samples:")
            for item in trace.get("rejected_records", [])[:3]:
                lines.append(f"    - {item.get('source_id')}: {item.get('title')} | {item.get('locator')}")
    if not state.get("local_rag_trace"):
        lines.append("- 无")
    return "\n".join(lines)



def _ensure_reference_section(content: str, state: AgentState) -> str:
    base = content.rstrip()
    references = _render_reference_list(state)
    if "## 引用列表" in base or "## 来源清单" in base or "## 参考资料" in base:
        return base
    return f"{base}\n\n{references}"



def _check_evidence_sufficiency(state: AgentState) -> tuple[bool, str]:
    """检查证据是否足够生成可靠报告。

    Returns:
        (is_sufficient, reason)
    """
    web_evidence = state.get("web_evidence", [])
    local_evidence = state.get("local_evidence", [])
    total = len(web_evidence) + len(local_evidence)

    # 有 3 条以上证据，认为充分
    if total >= 3:
        return True, ""

    # 0 条证据 → 直接不够
    if total == 0:
        return False, "没有从任何来源检索到可用证据"

    all_evidence = web_evidence + local_evidence
    scored = [item for item in all_evidence if isinstance(item, dict) and item.get("relevance_score") is not None]
    if scored:
        avg_relevance = sum(item["relevance_score"] for item in scored) / len(scored)
        max_relevance = max(item["relevance_score"] for item in scored)
    else:
        avg_relevance = 0.0
        max_relevance = 0.0

    # 只有 1-2 条证据且全部低相关
    if total <= 2 and max_relevance < 0.5:
        return False, (
            f"证据显著不足：召回 {total} 条证据，平均相关性 {avg_relevance:.2f}，"
            f"最高仅 {max_relevance:.2f}，无法生成有据可查的研报"
        )
    return True, ""



