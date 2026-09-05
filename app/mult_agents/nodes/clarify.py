"""clarify 节点（P4 HITL 启用：interrupt kind=clarification）。

P1 占位直通 plan → P4 改为真正 interrupt 节点：
- LLM（clarifier 角色）判断是否需要澄清 + 生成澄清问题列表
- 不需要澄清 → 直通 plan
- 需要澄清 → interrupt(kind=clarification) → 用户回答后判断是否充分
- 多轮澄清上限 2 次（防死循环）
"""
import logging
from typing import Literal

from langgraph.types import Command, StreamWriter

from ..state import AgentState
from ._shared import raise_interrupt, colorize

logger = logging.getLogger("mult_agents")


def clarify_node(state: AgentState, writer: StreamWriter = None) -> Command[Literal["plan", "clarify", "__end__"]] | dict:
    """澄清节点：P4 启用 interrupt。

    流程：
    1. 判断是否需要澄清（基于 query 复杂度/模糊度）
    2. 不需要 → 直通 plan
    3. 需要 → interrupt(kind=clarification) 向用户提问
    4. 用户回答后判断是否充分，不充分可多轮（上限 2 次）
    """
    query = state.get("query", "")
    clarifications = state.get("clarifications", [])

    logger.info("%s 开始 | query=%s | 轮次=%d", colorize("[clarify]", "cyan"), query[:80], len(clarifications))

    if writer:
        writer({"node": "clarify", "message": "正在判断是否需要澄清..."})

    # 多轮上限：已有 2 轮澄清记录 → 直通 plan
    if len(clarifications) >= 2:
        logger.info("[clarify] 已达多轮上限，直通 plan")
        return Command(goto="plan", update={})

    # 简单判断：query 长度 > 10 且含问号 → 可能需要澄清
    # 实际 P5+ 可换为 LLM 判断，此处用规则兜底
    needs_clarification = _check_needs_clarification(query, clarifications)

    if not needs_clarification:
        logger.info("[clarify] 无需澄清，直通 plan")
        return Command(goto="plan", update={})

    # 生成澄清问题列表
    questions = _generate_clarify_questions(query, clarifications)

    if not questions:
        return Command(goto="plan", update={})

    # interrupt 向用户提问
    if writer:
        writer({"node": "clarify", "message": "需要用户澄清问题"})

    answers = raise_interrupt("clarification", {
        "questions": questions,
        "message": "请回答以下问题以帮助更好地研究：",
    })

    # 记录澄清问答
    new_clarification = {"q": questions, "a": answers if isinstance(answers, list) else [str(answers)]}

    # 判断是否需要追问（简单规则：回答过短或空）
    followup_needed = _check_followup_needed(answers)

    if followup_needed and len(clarifications) < 1:
        # 需要追问且未达上限 → 回 clarify 再问一轮
        logger.info("[clarify] 回答不充分，需要追问")
        return Command(goto="clarify", update={
            "clarifications": [new_clarification],
        })

    # 澄清充分 → 进入 plan
    logger.info("[clarify] 澄清完成，进入 plan")
    return Command(goto="plan", update={
        "clarifications": [new_clarification],
    })


def _check_needs_clarification(query: str, clarifications: list) -> bool:
    """判断是否需要澄清（规则兜底，后续可换 LLM）。

    简单规则：
    - query 太短（<5 字）→ 需要澄清
    - query 含模糊词（"最近的"、"最新的"、"一些"等）→ 需要澄清
    - 已有澄清记录且回答充分 → 不需要
    """
    if len(query.strip()) < 5:
        return True

    ambiguous_keywords = ["最近的", "最新的", "一些", "相关", "类似", "比较好"]
    if any(kw in query for kw in ambiguous_keywords):
        return True

    return False


def _generate_clarify_questions(query: str, clarifications: list) -> list:
    """生成澄清问题列表。

    Returns:
        [{id, question, options?}] 格式的问题列表
    """
    questions = []

    if len(query.strip()) < 5:
        questions.append({
            "id": "q1",
            "question": f"您的问题「{query}」比较简短，能否提供更多细节？例如具体的研究方向或关注点。",
        })

    if any(kw in query for kw in ["最近的", "最新的"]):
        questions.append({
            "id": "q_time",
            "question": "您提到「最近/最新」，具体指哪个时间段？",
            "options": ["最近一周", "最近一个月", "最近半年", "最近一年"],
        })

    if any(kw in query for kw in ["一些", "相关", "类似"]):
        questions.append({
            "id": "q_scope",
            "question": "您希望研究覆盖的范围有多大？",
            "options": ["概览级（5-10个条目）", "详细级（20+条目深度分析）"],
        })

    return questions


def _check_followup_needed(answers) -> bool:
    """判断回答是否充分（简单规则：回答过短或空）。

    Args:
        answers: 用户回答（list[str] 或 str）

    Returns:
        True = 需要追问
    """
    if not answers:
        return True

    if isinstance(answers, str):
        return len(answers.strip()) < 3

    if isinstance(answers, list):
        for ans in answers:
            if not ans or len(str(ans).strip()) < 3:
                return True
        return False

    return False
