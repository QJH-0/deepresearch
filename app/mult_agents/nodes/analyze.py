"""nodes 包：拆分自 nodes.py（P1-2）。

纪律：纯搬迁，零行为变更。每个文件只负责一类节点或辅助函数。
"""
import json
import logging

from langchain_core.messages import HumanMessage
from langgraph.types import interrupt, StreamWriter

from ..state import AgentState
from ._shared import colorize, emit, collect_tool_calls, with_memory_context, log_inputs
from ._parsing import _invoke_json_agent
from ._fallbacks import _fallback_analysis, _check_evidence_sufficiency

logger = logging.getLogger("mult_agents")


def analyze_node(state: AgentState, agent, agent_name: str, writer: StreamWriter | None = None) -> AgentState:
    logger.info("%s 开始 | agent=%s", colorize("[analyze]", "cyan"), colorize(agent_name, "magenta"))
    if writer:
        writer({"node": "analyze", "message": "正在分析证据并生成结论..."})
    fallback = _fallback_analysis(state)
    payload, content, messages = _invoke_json_agent(
        state,
        "请基于证据池输出结论映射 JSON，并评估证据完备性：\n"
        f"原问题：{state['query']}\n"
        f"子问题：{json.dumps(state.get('sub_questions', []), ensure_ascii=False)}\n"
        f"证据池：{json.dumps(state.get('evidence_pool', []), ensure_ascii=False)}\n"
        f"审计标记：{json.dumps(state.get('audit_flags', []), ensure_ascii=False)}",
        agent,
        agent_name,
        "analyze",
        fallback,
        writer=writer,
    )
    findings = payload.get("findings") if isinstance(payload.get("findings"), list) else fallback["findings"]
    claim_map = payload.get("claim_map") if isinstance(payload.get("claim_map"), list) else fallback["claim_map"]
    needs_more_research = payload.get("needs_more_research", False)
    missing_gaps = payload.get("missing_gaps", [])
    analysis_summary = payload.get("analysis_summary", content)

    # ── HITL: 证据不足时向用户提问 ──
    user_feedback: dict = {}
    if (
        needs_more_research
        and state.get("hitl_enabled", False)
        and state.get("hitl_config", {}).get("analyze_clarify", True)
    ):
        interrupt_value = {
            "type": "analyze_clarify",
            "node": "analyze",
            "missing_gaps": missing_gaps,
            "analysis_summary": analysis_summary,
            "message": (
                "分析发现信息缺口，请选择操作：\n"
                "1. {\"action\": \"auto_search\"} → 自动补搜\n"
                "2. {\"action\": \"user_supply\", \"info\": \"已知信息\"} → 直接补充\n"
                "3. {\"action\": \"skip\"} → 跳过缺口直接出报告"
            ),
        }
        user_feedback = interrupt(interrupt_value)

        action = user_feedback.get("action", "auto_search") if isinstance(user_feedback, dict) else "auto_search"
        if action == "user_supply":
            user_info = user_feedback.get("info", "")
            analysis_summary = f"{analysis_summary}\n\n[用户补充信息] {user_info}"
            needs_more_research = False
            missing_gaps = []
        elif action == "skip":
            needs_more_research = False
            missing_gaps = []

    return {
        "analysis": analysis_summary,
        "findings": findings,
        "claim_map": claim_map,
        "needs_more_research": needs_more_research,
        "missing_gaps": missing_gaps,
        "messages": messages,
        "user_feedback": user_feedback,
    }



def reflect_node(state: AgentState, agent, agent_name: str, writer: StreamWriter | None = None) -> AgentState:
    logger.info("%s 开始 | agent=%s", colorize("[reflect]", "cyan"), colorize(agent_name, "magenta"))
    if writer:
        writer({"node": "reflect", "message": "正在生成补搜计划..."})
    
    missing_gaps = state.get("missing_gaps", [])
    log_inputs("reflect", agent_name, {"missing_gaps": str(missing_gaps)})
    
    fallback = {
        "reflection_summary": "默认补搜",
        "supplementary_queries": [{"section_id": "gap_1", "query": state["query"], "source_preference": "hybrid", "reason": "fallback"}]
    }
    
    prompt = (
        f"分析师指出当前证据不足以完全回答问题，存在以下信息缺口：\n{json.dumps(missing_gaps, ensure_ascii=False)}\n\n"
        f"原问题：{state['query']}\n"
        f"子问题：{json.dumps(state.get('sub_questions', []), ensure_ascii=False)}\n"
        f"已执行过的搜索计划：\n{json.dumps(state.get('search_plan', []), ensure_ascii=False)}\n"
        f"已执行过的补搜计划：\n{json.dumps(state.get('supplementary_queries', []), ensure_ascii=False)}\n\n"
        "请生成新的补搜计划以填补缺口。"
    )
    
    payload, content, messages = _invoke_json_agent(
        state,
        prompt,
        agent,
        agent_name,
        "reflect",
        fallback,
        writer=writer,
    )
    
    return {
        "iteration": state.get("iteration", 0) + 1,
        "supplementary_queries": payload.get("supplementary_queries", fallback["supplementary_queries"]),
        "messages": messages,
    }



