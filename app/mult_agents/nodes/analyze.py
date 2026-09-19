"""nodes 包：拆分自 nodes.py（P1-2）。

纪律：纯搬迁，零行为变更。每个文件只负责一类节点或辅助函数。
"""
import json
import logging

from langgraph.types import StreamWriter

from ..state import AgentState
from ._shared import colorize, raise_interrupt
from ._parsing import _invoke_structured_agent, StructuredOutputError
from ._fallbacks import _fallback_analysis
from ._context import build_research_note, evidence_for_prompt, render_research_notes

logger = logging.getLogger("mult_agents")


async def analyze_node(state: AgentState, agent, agent_name: str, writer: StreamWriter = None) -> AgentState:
    logger.info("%s 开始 | agent=%s", colorize("[analyze]", "cyan"), colorize(agent_name, "magenta"))
    if writer:
        writer({"node": "analyze", "message": "正在分析证据并生成结论..."})
    prompt = (
        "请基于证据池输出结论映射，并评估证据完备性：\n"
        f"原问题：{state['query']}\n"
        f"子问题：{json.dumps(state.get('sub_questions', []), ensure_ascii=False)}\n"
        f"证据池：{json.dumps(evidence_for_prompt(state), ensure_ascii=False)}\n"
        f"审计标记：{json.dumps(state.get('audit_flags', []), ensure_ascii=False)}\n"
        f"已执行过的检索词：{json.dumps(state.get('search_plan', []) + (state.get('supplementary_queries') or []), ensure_ascii=False)}\n"
        f"历史轮次研究笔记（已确认结论无需重查；低可信来源勿再作为主要依据）：\n{render_research_notes(state.get('research_notes', [])) or '（无）'}\n\n"
        "若判定证据不足（needs_more_research 为真），请针对每个缺口给出补检索词 gap_queries："
        "每个补检索词必须与已执行过的检索词不同，可换同义词、加限定词或补英文表述；"
        "证据已足够时 needs_more_research 为假且 gap_queries 留空。"
    )
    try:
        draft, messages = await _invoke_structured_agent(
            state, prompt, agent, agent_name, "analyze", writer=writer
        )
        findings = [claim.model_dump() for claim in draft.findings]
        claim_map = [entry.model_dump() for entry in draft.claim_map]
        needs_more_research = draft.needs_more_research
        missing_gaps = list(draft.missing_gaps)
        gap_queries = [item.model_dump() for item in draft.gap_queries]
        analysis_summary = draft.analysis_summary
    except StructuredOutputError as exc:
        # 降级到单条结论的兜底分析：报告仍可产出，但结论覆盖度明显下降
        logger.warning("%s 结构化输出失败，回退兜底分析 | %s", colorize("[analyze]", "yellow"), exc)
        fallback = _fallback_analysis(state)
        findings = fallback["findings"]
        claim_map = fallback["claim_map"]
        needs_more_research = False
        missing_gaps = []
        gap_queries = []
        analysis_summary = fallback["analysis_summary"]
        messages = []

    # ── HITL: 证据缺口处置（kind=evidence_gap，与 clarify 的 clarification 分离） ──
    user_feedback: dict = {}
    if (
        needs_more_research
        and state.get("hitl_enabled", False)
        and state.get("hitl_config", {}).get("analyze_clarify", True)
    ):
        # payload 携带 kind=evidence_gap；resume 值形如
        # {"kind": "evidence_gap", "action": "auto_search|user_supply|skip", "info": "..."}
        interrupt_payload = {
            "node": "analyze",
            "missing_gaps": missing_gaps,
            "analysis_summary": analysis_summary,
            "message": "分析发现信息缺口，请选择操作：自动补搜 / 补充信息 / 跳过缺口",
        }
        user_feedback = raise_interrupt("evidence_gap", interrupt_payload)

        action = user_feedback.get("action", "auto_search") if isinstance(user_feedback, dict) else "auto_search"
        if action == "user_supply":
            user_info = str(user_feedback.get("info", "")).strip()
            if user_info:
                analysis_summary = f"{analysis_summary}\n\n[用户补充信息] {user_info}"
            needs_more_research = False
            missing_gaps = []
        elif action == "skip":
            needs_more_research = False
            missing_gaps = []

    iteration = int(state.get("iteration", 0) or 0)
    max_iter = int(state.get("max_iterations", 2) or 0)
    # 是否继续研究：由 LLM 的 needs_more_research 与硬上限共同决定。
    # 上限是安全闸，不能被模型判断绕过；HITL 的 user_supply/skip 也会把前者置假。
    continue_research = needs_more_research and iteration < max_iter
    if needs_more_research and not continue_research:
        logger.info("[analyze] 已达迭代上限 %d，不再继续研究", max_iter)
    elif continue_research:
        logger.info("[analyze] 判定需继续研究 | 补检计划=%d 条 | iteration=%d→%d",
                    len(gap_queries), iteration, iteration + 1)

    # 研究笔记：只返回本轮这一条，累积由 reducer 负责
    note = build_research_note(state, findings, missing_gaps, state.get("audit_flags", []))
    return {
        "analysis": analysis_summary,
        "findings": findings,
        "claim_map": claim_map,
        "needs_more_research": needs_more_research,
        "missing_gaps": missing_gaps,
        # 路由只读这个字段，判定逻辑全部收在本节点内
        "next_action": "reflect" if continue_research else "write",
        "supplementary_queries": gap_queries if continue_research else [],
        # 只在继续研究时推进轮次：不推进会让上限闸永远不生效
        "iteration": iteration + 1 if continue_research else iteration,
        "research_notes": [note],
        "agent_messages": messages,
        "user_feedback": user_feedback,
    }
