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
from ._evidence import _default_plan, _derive_search_plan

logger = logging.getLogger("mult_agents")


def plan_node(state: AgentState, agent, agent_name: str, writer: StreamWriter | None = None) -> AgentState:
    logger.info("%s 开始 | agent=%s", colorize("[plan]", "cyan"), colorize(agent_name, "magenta"))
    if writer:
        writer({"node": "plan", "message": "正在生成研究计划..."})
    log_inputs("plan", agent_name, {"query": state["query"]})
    fallback = _default_plan(state)
    payload, content, messages = _invoke_json_agent(
        state,
        f"用户需求：{state['query']}\n请先做大纲与问题拆解，再输出规划 JSON。",
        agent,
        agent_name,
        "plan",
        fallback,
        writer=writer,
    )
    outline = payload.get("outline") if isinstance(payload.get("outline"), list) else fallback["outline"]
    sub_questions = payload.get("sub_questions") if isinstance(payload.get("sub_questions"), list) else fallback["sub_questions"]
    research_questions = payload.get("research_questions") if isinstance(payload.get("research_questions"), list) else fallback["research_questions"]
    budget = payload.get("budget") if isinstance(payload.get("budget"), dict) else fallback["budget"]
    search_plan = _derive_search_plan(outline, sub_questions, research_questions, state["query"])
    plan_summary = payload.get("objective") or state["query"]

    # ── HITL: 规划确认中断 ──
    user_feedback: dict = {}
    if state.get("hitl_enabled", False) and state.get("hitl_config", {}).get("plan_review", True):
        interrupt_value = {
            "type": "plan_review",
            "node": "plan",
            "plan": plan_summary,
            "outline": outline,
            "sub_questions": sub_questions,
            "search_plan": search_plan,
            "budget": budget,
            "message": "研究计划已生成，请确认或修改。",
        }
        user_feedback = interrupt(interrupt_value)

        if isinstance(user_feedback, dict) and not user_feedback.get("approved", True):
            feedback_text = user_feedback.get("feedback", "")
            if feedback_text:
                replan_prompt = (
                    f"原问题：{state['query']}\n"
                    f"初步计划：{plan_summary}\n"
                    f"用户修改意见：{feedback_text}\n"
                    "请根据用户意见调整计划，输出修订后的规划 JSON。"
                )
                payload, content, messages = _invoke_json_agent(
                    state, replan_prompt, agent, agent_name, "plan", fallback
                )
                outline = payload.get("outline") if isinstance(payload.get("outline"), list) else outline
                sub_questions = payload.get("sub_questions") if isinstance(payload.get("sub_questions"), list) else sub_questions
                research_questions = payload.get("research_questions") if isinstance(payload.get("research_questions"), list) else research_questions
                search_plan = _derive_search_plan(outline, sub_questions, research_questions, state["query"])
                plan_summary = payload.get("objective") or plan_summary

    return {
        "phase": "planning completed",
        "plan": plan_summary,
        "outline": outline,
        "sub_questions": sub_questions,
        "research_questions": research_questions,
        "search_plan": search_plan,
        "budget": budget,
        "messages": messages,
        "draft": content,
        "iteration": 0,
        "user_feedback": user_feedback,
    }

