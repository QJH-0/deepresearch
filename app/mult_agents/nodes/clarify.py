"""clarify 节点：双层判定（规则快速通道 + LLM 精判）。

R2.3 升级：
- 第一层：规则快速通道（短 query / 模糊词）→ 零 token 成本直接发起澄清
- 第二层：LLM 置信度自评 + 带选项的澄清问题生成
- 回答充分性由 LLM 判定，不充分则追问（受轮次上限约束）
- LLM 调用/解析失败 → 静默放行进入 plan（宁可不澄清也不能卡死研究流程）
"""

import json
import logging
import re
from typing import Literal

from langchain_core.messages import HumanMessage
from langgraph.types import Command, StreamWriter

from ..state import AgentState
from ._shared import colorize, raise_interrupt, with_memory_context

logger = logging.getLogger("mult_agents")

_CLARIFY_MAX_ROUNDS = 2

_AMBIGUOUS_KEYWORDS = ["最近的", "最新的", "一些", "相关", "类似", "比较好"]


# ── LLM prompt ──

CLARIFY_VERDICT_PROMPT = """你是研究需求分析专家。判断以下研究问题是否需要向用户澄清才能开展有效研究。

研究问题：{query}

{clarified_context}

评估维度：范围是否明确、术语是否可指代消歧、时间范围是否清楚、输出物要求是否明确。

要求：
1. confidence 为你对「已可开展研究」的置信度，0~1 小数
2. needs_clarification 为 true 时必须给出 1~3 个澄清问题，每个问题附 2~4 个选项
3. 只输出 JSON，不要其他文字

输出格式：
{{"needs_clarification": true, "confidence": 0.35, "reason": "时间范围不明确",
  "questions": [{{"question": "研究的时间范围是？", "options": ["近一年", "近三年", "不限"]}}]}}
"""

CLARIFY_SUFFICIENCY_PROMPT = """你是研究需求分析专家。用户已对研究问题做出澄清回答，判断信息是否充分。

研究问题：{query}
澄清问答记录：
{qa_pairs}

要求：
1. sufficient 为 true 表示信息已足够开展研究
2. 不充分时给出 1~2 个追问问题（附选项），只针对仍缺失的关键信息
3. 只输出 JSON，不要其他文字

输出格式：
{{"sufficient": false, "reason": "仍缺目标用户定位",
  "followup_questions": [{{"question": "...", "options": ["...", "..."]}}]}}
"""


# ── 规则快速通道 ──

def _rule_needs_clarification(query: str) -> bool:
    """规则快速通道：短 query 或含模糊词 → 直接判定需要澄清。"""
    if len(query.strip()) < 5:
        return True
    if any(kw in query for kw in _AMBIGUOUS_KEYWORDS):
        return True
    return False


def _rule_questions(query: str) -> list:
    """规则快速通道生成的澄清问题（带选项，向下兼容旧格式）。"""
    questions = []

    if len(query.strip()) < 5:
        questions.append({
            "id": "q1",
            "question": f"您的问题「{query}」比较简短，能否提供更多细节？例如具体的研究方向或关注点。",
            "options": [],
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


# ── LLM 判定函数 ──

def _extract_json(text: str) -> dict | None:
    """从 LLM 输出中提取 JSON：先直接 json.loads，失败则正则截取 {...} 再试。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        value = json.loads(cleaned)
        if isinstance(value, dict):
            return value
    except Exception:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            value = json.loads(cleaned[start : end + 1])
            if isinstance(value, dict):
                return value
        except Exception:
            pass
    return None


def _llm_clarify_verdict(agent, query: str, clarifications: list) -> dict | None:
    """调用 LLM 判定是否需要澄清。

    Returns:
        {"needs_clarification": bool, "confidence": float, "reason": str,
         "questions": [{"question": str, "options": [str]}]}
        解析失败或异常时返回 None（静默放行）。
    """
    try:
        clarified_context = ""
        if clarifications:
            qa_lines = []
            for c in clarifications:
                qs = c.get("q", [])
                ans = c.get("a", [])
                if isinstance(qs, list):
                    q_text = "; ".join(
                        q.get("question", str(q)) if isinstance(q, dict) else str(q)
                        for q in qs
                    )
                else:
                    q_text = str(qs)
                a_text = "; ".join(str(a) for a in ans) if isinstance(ans, list) else str(ans)
                qa_lines.append(f"Q: {q_text}\nA: {a_text}")
            clarified_context = "已澄清记录：\n" + "\n".join(qa_lines)

        prompt = CLARIFY_VERDICT_PROMPT.format(
            query=query, clarified_context=clarified_context
        )
        human = HumanMessage(content=with_memory_context({"query": query, "memory_context": ""}, prompt))
        result = agent.invoke({"messages": [human]})
        content = result["messages"][-1].content
        if isinstance(content, list):
            content = "\n".join(
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in content
            )
        data = _extract_json(content)
        if data is None:
            logger.warning("[clarify] LLM 判定 JSON 解析失败，静默放行")
            return None

        needs = bool(data.get("needs_clarification", False))
        confidence = data.get("confidence", 0.5)
        if isinstance(confidence, (int, float)):
            confidence = max(0.0, min(1.0, float(confidence)))
        else:
            confidence = 0.5

        questions = []
        if needs:
            raw_qs = data.get("questions", [])
            if not isinstance(raw_qs, list):
                raw_qs = []
            for q in raw_qs[:3]:
                if not isinstance(q, dict):
                    continue
                q_text = str(q.get("question", "")).strip()
                if not q_text:
                    continue
                opts = q.get("options", [])
                if not isinstance(opts, list):
                    opts = []
                opts = [str(o).strip() for o in opts if str(o).strip()][:4]
                questions.append({"question": q_text, "options": opts})
            if not questions:
                needs = False

        return {
            "needs_clarification": needs,
            "confidence": confidence,
            "reason": str(data.get("reason", "")),
            "questions": questions,
        }
    except Exception as exc:
        logger.warning("[clarify] LLM 判定异常，静默放行: %s", exc)
        return None


def _llm_answer_sufficiency(agent, query: str, clarifications: list) -> dict | None:
    """调用 LLM 判定回答是否充分。

    Returns:
        {"sufficient": bool, "reason": str,
         "followup_questions": [{"question": str, "options": [str]}]}
        解析失败或异常时返回 None（按充分处理，放行不卡死）。
    """
    try:
        qa_lines = []
        for c in clarifications:
            qs = c.get("q", [])
            ans = c.get("a", [])
            if isinstance(qs, list):
                q_text = "; ".join(
                    q.get("question", str(q)) if isinstance(q, dict) else str(q)
                    for q in qs
                )
            else:
                q_text = str(qs)
            a_text = "; ".join(str(a) for a in ans) if isinstance(ans, list) else str(ans)
            qa_lines.append(f"Q: {q_text}\nA: {a_text}")
        qa_pairs = "\n".join(qa_lines) if qa_lines else "（暂无）"

        prompt = CLARIFY_SUFFICIENCY_PROMPT.format(query=query, qa_pairs=qa_pairs)
        human = HumanMessage(content=with_memory_context({"query": query, "memory_context": ""}, prompt))
        result = agent.invoke({"messages": [human]})
        content = result["messages"][-1].content
        if isinstance(content, list):
            content = "\n".join(
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in content
            )
        data = _extract_json(content)
        if data is None:
            logger.warning("[clarify] LLM 充分性 JSON 解析失败，按充分放行")
            return None

        sufficient = bool(data.get("sufficient", True))
        followup = []
        if not sufficient:
            raw_qs = data.get("followup_questions", [])
            if not isinstance(raw_qs, list):
                raw_qs = []
            for q in raw_qs[:2]:
                if not isinstance(q, dict):
                    continue
                q_text = str(q.get("question", "")).strip()
                if not q_text:
                    continue
                opts = q.get("options", [])
                if not isinstance(opts, list):
                    opts = []
                opts = [str(o).strip() for o in opts if str(o).strip()][:4]
                followup.append({"question": q_text, "options": opts})

        return {
            "sufficient": sufficient,
            "reason": str(data.get("reason", "")),
            "followup_questions": followup,
        }
    except Exception as exc:
        logger.warning("[clarify] LLM 充分性判定异常，按充分放行: %s", exc)
        return None


# ── 节点主函数 ──

def clarify_node(
    state: AgentState,
    agent=None,
    agent_name: str = "",
    writer: StreamWriter = None,
) -> Command[Literal["plan", "clarify", "__end__"]] | dict:
    """澄清节点：双层判定（规则快速通道 + LLM 精判）。

    流程：
    1. 规则快速通道命中 → 直接发起澄清（零 token 成本）
    2. 轮次上限 → 直通 plan
    3. LLM 精判需要澄清 → 发起带选项的澄清中断
    4. 用户回答后 LLM 判定充分性，不充分则追问（受轮次限制）
    """
    query = state.get("query", "")
    clarifications = state.get("clarifications", [])
    rounds = state.get("clarify_rounds", 0)
    hitl_config = state.get("hitl_config") or {}
    max_rounds = hitl_config.get("clarify_max_rounds", _CLARIFY_MAX_ROUNDS)

    logger.info(
        "%s 开始 | query=%s | 轮次=%d | agent=%s",
        colorize("[clarify]", "cyan"),
        query[:80],
        rounds,
        agent_name or "None",
    )

    if writer:
        writer({"node": "clarify", "message": "正在判断是否需要澄清..."})

    # ① 规则快速通道（零成本短路，仅在首轮且无已有回答时生效）
    if not clarifications and _rule_needs_clarification(query):
        logger.info("[clarify] 规则快速通道命中，直接发起澄清")
        questions = _rule_questions(query)
        if writer:
            writer({"node": "clarify", "message": "需要用户澄清问题"})
        answers = raise_interrupt("clarification", {
            "questions": questions,
            "message": "请回答以下问题以帮助更好地研究：",
        })
        new_clarification = {
            "q": questions,
            "a": answers if isinstance(answers, list) else [str(answers)],
        }
        logger.info("[clarify] 规则通道回答已记录，进入 plan")
        return Command(goto="plan", update={
            "clarifications": [new_clarification],
            "clarify_rounds": rounds + 1,
        })

    # ② 轮次上限检查
    if rounds >= max_rounds:
        logger.info("[clarify] 已达轮次上限(%d)，直通 plan", max_rounds)
        return Command(goto="plan", update={})

    # ③ 有回答 → LLM 判定充分性；无回答 → LLM 判定是否需要澄清
    if clarifications:
        return _handle_sufficiency_check(
            state, agent, query, clarifications, rounds, max_rounds, writer
        )
    else:
        return _handle_initial_verdict(
            state, agent, query, rounds, writer
        )


def _handle_initial_verdict(
    state: AgentState,
    agent,
    query: str,
    rounds: int,
    writer: StreamWriter = None,
) -> Command[Literal["plan", "clarify"]] | dict:
    """首轮 LLM 判定：是否需要澄清。"""
    if agent is None:
        logger.info("[clarify] 无 agent，直通 plan")
        return Command(goto="plan", update={})

    verdict = _llm_clarify_verdict(agent, query, [])
    if verdict is None:
        logger.info("[clarify] LLM 判定失败，静默放行进 plan")
        return Command(goto="plan", update={})

    if not verdict["needs_clarification"]:
        logger.info("[clarify] LLM 判定无需澄清(conf=%.2f)，直通 plan", verdict["confidence"])
        return Command(goto="plan", update={})

    questions = verdict["questions"]
    if writer:
        writer({"node": "clarify", "message": "需要用户澄清问题"})

    answers = raise_interrupt("clarification", {
        "questions": questions,
        "message": "请回答以下问题以帮助更好地研究：",
    })

    new_clarification = {
        "q": questions,
        "a": answers if isinstance(answers, list) else [str(answers)],
    }
    logger.info("[clarify] LLM 首轮澄清回答已记录，进入充分性判定")
    return Command(goto="clarify", update={
        "clarifications": [new_clarification],
        "clarify_rounds": rounds + 1,
    })


def _handle_sufficiency_check(
    state: AgentState,
    agent,
    query: str,
    clarifications: list,
    rounds: int,
    max_rounds: int,
    writer: StreamWriter = None,
) -> Command[Literal["plan", "clarify"]] | dict:
    """回答充分性判定：充分 → plan；不充分且未超轮次 → 追问。"""
    if agent is None:
        logger.info("[clarify] 无 agent，直通 plan")
        return Command(goto="plan", update={})

    sufficiency = _llm_answer_sufficiency(agent, query, clarifications)
    if sufficiency is None or sufficiency["sufficient"]:
        logger.info("[clarify] 回答充分(或判定失败按充分)，进入 plan")
        return Command(goto="plan", update={})

    if rounds >= max_rounds:
        logger.info("[clarify] 不充分但已达轮次上限(%d)，放行进 plan", max_rounds)
        return Command(goto="plan", update={})

    followup = sufficiency.get("followup_questions", [])
    if not followup:
        logger.info("[clarify] 不充分但无追问问题，放行进 plan")
        return Command(goto="plan", update={})

    if writer:
        writer({"node": "clarify", "message": "信息不充分，需要追问"})

    answers = raise_interrupt("clarification", {
        "questions": followup,
        "message": "还需补充以下信息：",
    })

    new_clarification = {
        "q": followup,
        "a": answers if isinstance(answers, list) else [str(answers)],
    }
    logger.info("[clarify] 追问回答已记录(轮次→%d)", rounds + 1)
    return Command(goto="clarify", update={
        "clarifications": [new_clarification],
        "clarify_rounds": rounds + 1,
    })
