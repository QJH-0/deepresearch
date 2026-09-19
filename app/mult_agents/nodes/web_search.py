"""nodes 包：拆分自 nodes.py（P1-2）。

纪律：纯搬迁，零行为变更。每个文件只负责一类节点或辅助函数。
"""
import asyncio
import json
import logging

from langchain_core.messages import HumanMessage
from langgraph.types import StreamWriter

from ..state import AgentState
from ..tools import web_search_records
from ._shared import colorize, log_inputs
from ._parsing import StructuredOutputError, _invoke_structured_agent
from ._evidence import (
    _build_queries, _assign_source_ids, _dedupe_sources, _minimal_record_filter,
    _summarize_records, _format_raw_records, _fallback_web_evidence,
    _prune_evidence_to_allowed_sources, _enrich_evidence_from_raw,
    _finalize_query_traces, _filter_web_records, _retrieval_pass_index,
)

logger = logging.getLogger("mult_agents")


async def web_search_node(state: AgentState, agent, agent_name: str, writer: StreamWriter = None) -> AgentState:
    logger.info("%s 开始 | agent=%s", colorize("[web_search]", "cyan"), colorize(agent_name, "magenta"))
    queries = _build_queries(state, "web")
    logger.info("[web_search_node] 构建查询 | 查询数量=%s | queries=%s", len(queries), [q.get("query", "") for q in queries])
    if writer:
        writer({"node": "web_search", "message": f"Web Scout 开始检索，共 {len(queries)} 个查询"})
    
    raw_records = []
    # 只收集本轮轨迹：web_search_trace 是累积型字段，拼接由 reducer 负责
    query_traces = []
    
    iteration = state.get("iteration", 0)
    # 用检索批次号而非 iteration：内层重检会多次检索，用 iteration 会撞 ID
    prefix = f"WEB{_retrieval_pass_index(state)}"
    logger.info("[web_search_node] 迭代信息 | iteration=%s | retrieval_round=%s | prefix=%s",
                iteration, state.get("retrieval_round", 0), prefix)
    
    for query_index, item in enumerate(queries, 1):
        query_text = str(item.get("query", ""))
        logger.info("[web_search_node] 执行第 %s/%s 个查询 | query=%s | section_id=%s", query_index, len(queries), query_text, item.get("section_id"))
        if writer:
            writer({"node": "web_search", "message": f"正在执行第 {query_index}/{len(queries)} 个查询: {query_text[:50]}"})
        # 搜索链是同步阻塞实现（内部 future.result 等待），放到线程池执行，
        # 否则会阻塞事件循环、拖死同进程其他 SSE 流
        records = await asyncio.to_thread(web_search_records, query_text, 4)
        logger.info("[web_search_node] 查询 %s 返回 | 记录数=%s", query_index, len(records))
        if writer:
            writer({"node": "web_search", "message": f"查询 {query_index} 返回 {len(records)} 条结果"})
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
                "source_preference": item.get("source_preference", "web"),
                "raw_count": len(records),
                "raw_records": _summarize_records(records),
            }
        )
    raw_records = _dedupe_sources(raw_records, ["url", "title"])
    raw_records = _minimal_record_filter(raw_records, ["title", "snippet", "url"])
    # 相关性硬过滤：写入 relevance_score 供证据充分性判定使用，并剔除无关/垃圾域名结果
    raw_records, relevance_stats = _filter_web_records(state["query"], raw_records)
    logger.info("[web_search_node] 相关性过滤后 | 保留=%s | 丢弃无关=%s | 丢弃域名=%s",
                len(raw_records), relevance_stats["dropped_irrelevant"], relevance_stats["dropped_domain"])
    logger.info("[web_search_node] 数据清洗后 | 去重过滤后记录数=%s", len(raw_records))
    
    web_retrieval_stats = state.get("web_retrieval_stats", {})
    web_retrieval_stats["query_count"] = web_retrieval_stats.get("query_count", 0) + len(queries)
    web_retrieval_stats["raw_count"] = web_retrieval_stats.get("raw_count", 0) + len(raw_records)
    web_retrieval_stats["dropped_irrelevant"] = (
        web_retrieval_stats.get("dropped_irrelevant", 0) + relevance_stats["dropped_irrelevant"]
    )
    web_retrieval_stats["dropped_domain"] = (
        web_retrieval_stats.get("dropped_domain", 0) + relevance_stats["dropped_domain"]
    )
    
    log_inputs("web_search", agent_name, {"query_count": str(len(queries)), "raw_count": str(len(raw_records))})
    if not raw_records:
        logger.warning("[web_search_node] 无可用网页证据，跳过网页上下文注入 | 查询数=%s", len(queries))
        logger.info("%s 无可用网页证据，跳过网页上下文注入", colorize("[web_search]", "yellow"))
        # 不返回 web_evidence：无新证据时回写旧值，reducer 会执行「旧值 + 旧值」把证据翻倍
        return {
            "web_retrieval_stats": web_retrieval_stats,
            "web_search_trace": query_traces,
        }
    logger.info("[web_search_node] 调用 LLM 整理证据 | raw_records=%s", len(raw_records))
    if writer:
        writer({"node": "web_search", "message": f"检索完成，共 {len(raw_records)} 条原始记录，正在用 LLM 整理证据..."})
    fallback = _fallback_web_evidence(raw_records)
    prompt = (
        "请基于以下网页证据整理结构化结果。\n"
        f"原问题：{state['query']}\n"
        f"子问题：{json.dumps(state.get('sub_questions', []), ensure_ascii=False)}\n"
        f"原始网页证据：\n{_format_raw_records(raw_records, 'web')}"
    )
    try:
        draft, messages = await _invoke_structured_agent(
            state, prompt, agent, agent_name, "web_search", writer=writer
        )
        payload = draft.model_dump()
    except StructuredOutputError as exc:
        # 降级用原始记录直接构造证据，保证检索结果不因整理失败而丢失；但必须留痕
        logger.warning("[web_search_node] 结构化整理失败，降级用原始记录构造证据 | %s", exc)
        if writer:
            writer({"node": "web_search", "message": "证据整理失败，改用原始记录构造"})
        payload, messages = fallback, []
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), list) else fallback["evidence"]
    logger.info("[web_search_node] LLM 返回证据 | evidence数量=%s", len(evidence))
    allowed_source_ids = {str(item.get("source_id")) for item in raw_records if item.get("source_id")}
    evidence = _prune_evidence_to_allowed_sources(evidence, allowed_source_ids)
    # LLM 整理后 evidence 为空但 raw_records 非空时，降级使用 fallback（直接从 raw_records 构造）
    if not evidence and raw_records:
        logger.warning("[web_search_node] LLM 整理后 evidence 为空，降级使用 fallback | raw_records=%s", len(raw_records))
        evidence = fallback["evidence"]
        evidence = _prune_evidence_to_allowed_sources(evidence, allowed_source_ids)
    # 从原始记录补充 LLM 可能丢失的 url/domain/title 字段
    evidence = _enrich_evidence_from_raw(evidence, raw_records)
    
    web_retrieval_stats["kept_count"] = web_retrieval_stats.get("kept_count", 0) + len(evidence)
    web_retrieval_stats["dropped_count"] = web_retrieval_stats.get("dropped_count", 0) + max(len(raw_records) - len(evidence), 0)
    
    kept_ids = {str(item.get("source_id")) for item in evidence if item.get("source_id")}
    query_traces = _finalize_query_traces(
        query_traces,
        kept_ids,
        payload.get("rejected_source_ids", []),
        str(payload.get("reject_reason", "")).strip(),
    )
    
    existing_count = len(state.get("web_evidence", []))
    logger.info("[web_search_node] 节点完成 | 新增证据=%s | 累计证据=%s", len(evidence), existing_count + len(evidence))
    if writer:
        writer({"node": "web_search", "message": f"Web检索完成：新增 {len(evidence)} 条证据，累计 {existing_count + len(evidence)} 条"})
        # P7-1: 发送 sources.found 事件（本轮新增来源）
        new_sources = [
            {
                "url": item.get("url", ""),
                "title": item.get("title", ""),
                "snippet": str(item.get("snippet", ""))[:200],
                "source_type": "web",
                "chunk_id": None,
            }
            for item in evidence
            if item.get("source_id")
        ]
        if new_sources:
            writer({"type": "sources", "sources": new_sources})
    return {
        # 只返回本轮新增：web_evidence 是累积型字段，与历史证据的拼接由 reducer 负责
        "web_evidence": evidence,
        "web_retrieval_stats": web_retrieval_stats,
        "web_search_trace": query_traces,
        "agent_messages": messages,
    }

