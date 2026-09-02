"""nodes 包：拆分自 nodes.py（P1-2）。

纪律：纯搬迁，零行为变更。每个文件只负责一类节点或辅助函数。
"""
import json
import logging

from langchain_core.messages import HumanMessage
from langgraph.types import StreamWriter

from ..state import AgentState
from ..tools import search_knowledge_base_records
from ._shared import colorize, emit, collect_tool_calls, with_memory_context, log_inputs
from ._parsing import _invoke_json_agent
from ._evidence import (
    _build_queries, _assign_source_ids, _dedupe_sources, _minimal_record_filter,
    _summarize_records, _format_raw_records, _fallback_local_evidence,
    _prune_evidence_to_allowed_sources, _enrich_evidence_from_raw,
    _finalize_query_traces,
)

logger = logging.getLogger("mult_agents")


def local_rag_node(state: AgentState, agent, agent_name: str, writer: StreamWriter | None = None) -> AgentState:
    logger.info("%s 开始 | agent=%s", colorize("[local_rag]", "cyan"), colorize(agent_name, "magenta"))
    queries = _build_queries(state, "local")
    if writer:
        writer({"node": "local_rag", "message": f"本地知识库检索开始，共 {len(queries)} 个查询"})
    raw_records = []
    query_traces = state.get("local_rag_trace", [])
    
    iteration = state.get("iteration", 0)
    prefix = f"LOC{iteration+1}"
    
    for query_index, item in enumerate(queries, 1):
        if writer:
            writer({"node": "local_rag", "message": f"正在检索本地知识库 {query_index}/{len(queries)}: {str(item.get('query', ''))[:50]}"})
        records = search_knowledge_base_records(str(item.get("query", "")), limit=4)
        records = _assign_source_ids(records, f"{prefix}_{query_index}")
        for record in records:
            record["section_id"] = item.get("section_id")
            record["search_query"] = item.get("query")
        raw_records.extend(records)
        query_traces.append(
            {
                "iteration": iteration,
                "plan_step": query_index,
                "query": str(item.get("query", "")),
                "section_id": item.get("section_id"),
                "reason": item.get("reason", ""),
                "source_preference": item.get("source_preference", "local"),
                "raw_count": len(records),
                "raw_records": _summarize_records(records),
            }
        )
    raw_records = _dedupe_sources(raw_records, ["doc_id", "snippet"])
    raw_records = _minimal_record_filter(raw_records, ["snippet", "title", "doc_id"])
    
    local_retrieval_stats = state.get("local_retrieval_stats", {})
    local_retrieval_stats["query_count"] = local_retrieval_stats.get("query_count", 0) + len(queries)
    local_retrieval_stats["raw_count"] = local_retrieval_stats.get("raw_count", 0) + len(raw_records)
    
    log_inputs("local_rag", agent_name, {"query_count": str(len(queries)), "raw_count": str(len(raw_records))})
    if writer:
        writer({"node": "local_rag", "message": f"本地检索完成：召回 {len(raw_records)} 条原始记录"})
    if not raw_records:
        logger.info("%s 无可用本地证据，跳过本地上下文注入", colorize("[local_rag]", "yellow"))
        return {
            "local_rag": "未检索到可用本地知识库证据，已跳过本地上下文注入。",
            "local_evidence": state.get("local_evidence", []),
            "local_retrieval_stats": local_retrieval_stats,
            "local_rag_trace": query_traces,
        }
    fallback = _fallback_local_evidence(raw_records)
    payload, content, messages = _invoke_json_agent(
        state,
        "请基于以下知识库证据整理结构化 JSON。\n"
        f"原问题：{state['query']}\n"
        f"子问题：{json.dumps(state.get('sub_questions', []), ensure_ascii=False)}\n"
        f"原始知识库证据：\n{_format_raw_records(raw_records, 'local')}",
        agent,
        agent_name,
        "local_rag",
        fallback,
        writer=writer,
    )
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), list) else fallback["evidence"]
    allowed_source_ids = {str(item.get("source_id")) for item in raw_records if item.get("source_id")}
    evidence = _prune_evidence_to_allowed_sources(evidence, allowed_source_ids)
    
    local_retrieval_stats["kept_count"] = local_retrieval_stats.get("kept_count", 0) + len(evidence)
    local_retrieval_stats["dropped_count"] = local_retrieval_stats.get("dropped_count", 0) + max(len(raw_records) - len(evidence), 0)
    
    kept_ids = {str(item.get("source_id")) for item in evidence if item.get("source_id")}
    query_traces = _finalize_query_traces(
        query_traces,
        kept_ids,
        payload.get("rejected_source_ids", []),
        str(payload.get("reject_reason", "")).strip(),
    )
    
    existing_evidence = state.get("local_evidence", [])
    return {
        "local_rag": payload.get("summary", content),
        "local_evidence": existing_evidence + evidence,
        "local_retrieval_stats": local_retrieval_stats,
        "local_rag_trace": query_traces,
        "messages": messages,
    }

