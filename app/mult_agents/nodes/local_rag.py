"""nodes 包：拆分自 nodes.py（P1-2）。

纪律：纯搬迁，零行为变更。每个文件只负责一类节点或辅助函数。
"""
import asyncio
import json
import logging

from langchain_core.messages import HumanMessage
from langgraph.types import StreamWriter

from ..state import AgentState
from ..tools import search_knowledge_base_records
from ._shared import colorize, log_inputs
from ._parsing import StructuredOutputError, _invoke_structured_agent
from ._evidence import (
    _build_queries, _assign_source_ids, _dedupe_sources, _minimal_record_filter,
    _summarize_records, _format_raw_records, _fallback_local_evidence,
    _prune_evidence_to_allowed_sources, _enrich_evidence_from_raw,
    _finalize_query_traces, _filter_local_records, _retrieval_pass_index,
)

logger = logging.getLogger("mult_agents")


async def local_rag_node(state: AgentState, agent, agent_name: str, writer: StreamWriter = None) -> AgentState:
    logger.info("%s 开始 | agent=%s", colorize("[local_rag]", "cyan"), colorize(agent_name, "magenta"))
    queries = _build_queries(state, "local")
    if writer:
        writer({"node": "local_rag", "message": f"本地知识库检索开始，共 {len(queries)} 个查询"})
    raw_records = []
    # 只收集本轮轨迹：local_rag_trace 是累积型字段，拼接由 reducer 负责
    query_traces = []
    
    iteration = state.get("iteration", 0)
    # 用检索批次号而非 iteration：内层重检会多次检索，用 iteration 会撞 ID
    prefix = f"LOC{_retrieval_pass_index(state)}"
    
    for query_index, item in enumerate(queries, 1):
        if writer:
            writer({"node": "local_rag", "message": f"正在检索本地知识库 {query_index}/{len(queries)}: {str(item.get('query', ''))[:50]}"})
        # RAG 检索链路内部为同步实现（LLM 重写 + Milvus + rerank），
        # 必须放到线程池执行，否则会阻塞事件循环、拖死同进程其他 SSE 流
        records = await asyncio.to_thread(
            search_knowledge_base_records, str(item.get("query", "")), 4
        )
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
    # 相关性硬过滤：写入 relevance_score 供证据充分性判定使用，并剔除低相关文档
    raw_records, relevance_stats = _filter_local_records(state["query"], raw_records)
    logger.info("[local_rag_node] 相关性过滤后 | 保留=%s | 丢弃无关=%s",
                len(raw_records), relevance_stats["dropped_irrelevant"])
    
    local_retrieval_stats = state.get("local_retrieval_stats", {})
    local_retrieval_stats["query_count"] = local_retrieval_stats.get("query_count", 0) + len(queries)
    local_retrieval_stats["raw_count"] = local_retrieval_stats.get("raw_count", 0) + len(raw_records)
    
    log_inputs("local_rag", agent_name, {"query_count": str(len(queries)), "raw_count": str(len(raw_records))})
    if writer:
        writer({"node": "local_rag", "message": f"本地检索完成：召回 {len(raw_records)} 条原始记录"})
    if not raw_records:
        logger.info("%s 无可用本地证据，跳过本地上下文注入", colorize("[local_rag]", "yellow"))
        # 不返回 local_evidence：无新证据时回写旧值，reducer 会执行「旧值 + 旧值」把证据翻倍
        return {
            "local_retrieval_stats": local_retrieval_stats,
            "local_rag_trace": query_traces,
        }
    fallback = _fallback_local_evidence(raw_records)
    prompt = (
        "请基于以下知识库证据整理结构化结果。\n"
        f"原问题：{state['query']}\n"
        f"子问题：{json.dumps(state.get('sub_questions', []), ensure_ascii=False)}\n"
        f"原始知识库证据：\n{_format_raw_records(raw_records, 'local')}"
    )
    try:
        draft, messages = await _invoke_structured_agent(
            state, prompt, agent, agent_name, "local_rag", writer=writer
        )
        payload = draft.model_dump()
    except StructuredOutputError as exc:
        # 降级用原始记录直接构造证据，保证检索结果不因整理失败而丢失；但必须留痕
        logger.warning("[local_rag_node] 结构化整理失败，降级用原始记录构造证据 | %s", exc)
        if writer:
            writer({"node": "local_rag", "message": "证据整理失败，改用原始记录构造"})
        payload, messages = fallback, []
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), list) else fallback["evidence"]
    allowed_source_ids = {str(item.get("source_id")) for item in raw_records if item.get("source_id")}
    evidence = _prune_evidence_to_allowed_sources(evidence, allowed_source_ids)
    
    local_retrieval_stats["kept_count"] = local_retrieval_stats.get("kept_count", 0) + len(evidence)
    local_retrieval_stats["dropped_count"] = local_retrieval_stats.get("dropped_count", 0) + max(len(raw_records) - len(evidence), 0)
    local_retrieval_stats["dropped_irrelevant"] = (
        local_retrieval_stats.get("dropped_irrelevant", 0) + relevance_stats["dropped_irrelevant"]
    )
    # 透传 url/doc_id/relevance_score 等原始字段（与 web_search_node 一致）
    evidence = _enrich_evidence_from_raw(evidence, raw_records)
    
    kept_ids = {str(item.get("source_id")) for item in evidence if item.get("source_id")}
    query_traces = _finalize_query_traces(
        query_traces,
        kept_ids,
        payload.get("rejected_source_ids", []),
        str(payload.get("reject_reason", "")).strip(),
    )
    
    if writer:
        # P7-1: 发送 sources.found 事件（本轮新增来源）
        new_sources = [
            {
                "url": None,
                "title": item.get("title", "") or item.get("doc_id", ""),
                "snippet": str(item.get("snippet", ""))[:200],
                "source_type": "kb",
                "chunk_id": item.get("doc_id", ""),
            }
            for item in evidence
            if item.get("source_id")
        ]
        if new_sources:
            writer({"type": "sources", "sources": new_sources})
    return {
        # 只返回本轮新增：local_evidence 是累积型字段，与历史证据的拼接由 reducer 负责
        "local_evidence": evidence,
        "local_retrieval_stats": local_retrieval_stats,
        "local_rag_trace": query_traces,
        "agent_messages": messages,
    }

