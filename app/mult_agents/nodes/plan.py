"""plan 节点（P4: plan_approval 三分支 approve/revise/reject）。

P4 改造：
- interrupt(kind=plan_approval) 载荷含 kind 键
- approve → 计划固化，进入检索
- revise + reason → 回 plan 节点重新生成（轮次上限 3）
- reject → END，保留已生成内容
- State 新增 plan_revision_count 防死循环
"""
import json
import logging

from langchain_core.messages import HumanMessage
from langgraph.types import StreamWriter, Command

from ..state import AgentState
from ._shared import colorize, emit, collect_tool_calls, with_memory_context, log_inputs, raise_interrupt
from ._parsing import _invoke_json_agent
from ._evidence import _default_plan, _derive_search_plan

logger = logging.getLogger("mult_agents")


def plan_node(state: AgentState, agent, agent_name: str, writer: StreamWriter = None) -> AgentState | Command:
    logger.info("%s 开始 | agent=%s", colorize("[plan]", "cyan"), colorize(agent_name, "magenta"))
    if writer:
        writer({"node": "plan", "message": "正在生成研究计划..."})
    log_inputs("plan", agent_name, {"query": state["query"]})
    fallback = _default_plan(state)

    # 如果是 revise 回环，携带修改原因
    revision_reason = state.get("user_feedback", {}).get("feedback", "") if isinstance(state.get("user_feedback"), dict) else ""
    plan_revision_count = state.get("plan_revision_count", 0)

    prompt = f"用户需求：{state['query']}\n请先做大纲与问题拆解，再输出规划 JSON。"
    if revision_reason:
        prompt = (
            f"原问题：{state['query']}\n"
            f"用户修改意见：{revision_reason}\n"
            f"上一版计划已生成但不满足需求，请根据用户意见调整计划，输出修订后的规划 JSON。"
        )

    payload, content, messages = _invoke_json_agent(
        state,
        prompt,
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

    # ── HITL: plan_approval 三分支 ──
    if state.get("hitl_enabled", False) and state.get("hitl_config", {}).get("plan_review", True):
        # 构造子问题列表（结构化）
        plan_items = [
            {"id": i, "question": q, "rationale": "", "source_hint": ""}
            for i, q in enumerate(sub_questions)
        ]

        decision = raise_interrupt("plan_approval", {
            "sub_questions": plan_items,
            "plan": plan_summary,
            "outline": outline,
            "search_plan": search_plan,
            "budget": budget,
            "revision_count": plan_revision_count,
            "message": "研究计划已生成，请确认、修改或否决。",
        })

        action = decision.get("action", "approve") if isinstance(decision, dict) else "approve"
        reason = decision.get("reason", "") if isinstance(decision, dict) else ""

        match action:
            case "approve":
                logger.info("[plan] 用户批准计划")
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
                    "plan_revision_count": 0,
                    "user_feedback": {"approved": True},
                }

            case "revise":
                # 轮次上限检查
                if plan_revision_count >= 3:
                    logger.warning("[plan] 修改已达上限 %d 次，强制采纳", plan_revision_count)
                    if writer:
                        writer({"node": "plan", "message": "已达修改上限，计划自动采纳"})
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
                        "plan_revision_count": plan_revision_count,
                        "user_feedback": {"approved": True, "reason": "max_revisions_reached"},
                    }

                logger.info("[plan] 用户要求修改 | reason=%s | revision=%d", reason, plan_revision_count + 1)
                # 回 plan 节点重新生成
                return Command(goto="plan", update={
                    "plan": "",
                    "plan_revision_count": plan_revision_count + 1,
                    "user_feedback": {"approved": False, "feedback": reason},
                })

            case "reject":
                logger.info("[plan] 用户否决计划")
                return Command(goto="__end__", update={
                    "final": "研究计划被否决。",
                    "needs_more_research": False,
                    "plan": plan_summary,
                    "draft": content,
                    "user_feedback": {"rejected": True, "reason": reason},
                })

    # HITL 未启用 → 直通
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
        "user_feedback": {},
        "plan_revision_count": 0,
    }
