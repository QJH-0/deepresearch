"""检索充分性裁判节点：检索阶段的**内层**自适应循环。

与 `reflect` 的分工（层级不同，别混用）：

- 本节点判「**这一轮**检索够不够，不够就换个词再搜」—— 检索阶段内部循环
- `reflect` 判「跨轮次还要不要继续研究」—— 外层循环

降级策略：裁判失败时**放行**而不是重试。重检需要新的检索词，而检索词正是裁判的
产出——裁判都失败了，循环没有可用输入，继续转只会空烧检索配额。
放行必须留痕（`retrieval_grade.degraded`），否则自适应能力被静默关闭。
"""

import json
import logging

from langgraph.types import StreamWriter

from ..state import AgentState
from ._shared import colorize, log_inputs
from ._parsing import StructuredOutputError, _invoke_structured_agent
from ._context import evidence_for_prompt

logger = logging.getLogger("mult_agents")


async def retrieval_grader_node(
    state: AgentState, agent, agent_name: str, writer: StreamWriter = None
) -> AgentState:
    logger.info("%s 开始 | agent=%s", colorize("[retrieve_grader]", "cyan"), colorize(agent_name, "magenta"))
    round_index = int(state.get("retrieval_round", 0) or 0)
    max_rounds = int(state.get("max_retrieval_rounds", 2) or 0)
    log_inputs("retrieve_grader", agent_name, {"round": str(round_index), "max_rounds": str(max_rounds)})
    if writer:
        writer({"node": "retrieve_grader", "message": f"正在判定检索充分性（第 {round_index + 1} 轮）..."})

    executed_queries = state.get("retrieval_queries") or state.get("search_plan") or []
    prompt = (
        "请判定当前检索结果是否足以支撑后续分析。\n"
        f"原问题：{state['query']}\n"
        f"子问题：{json.dumps(state.get('sub_questions', []), ensure_ascii=False)}\n"
        f"已执行的检索词：{json.dumps(executed_queries, ensure_ascii=False)}\n"
        f"已召回证据：{json.dumps(evidence_for_prompt(state), ensure_ascii=False)}"
    )

    try:
        draft, messages = await _invoke_structured_agent(
            state, prompt, agent, agent_name, "retrieve_grader", writer=writer
        )
        sufficient = bool(draft.sufficient)
        gaps = [str(gap) for gap in draft.gaps if str(gap).strip()]
        rewritten = [item.model_dump() for item in draft.rewritten_queries]
    except StructuredOutputError as exc:
        logger.warning("%s 裁判失败，放行进入分析（本轮自适应重检未生效） | %s",
                       colorize("[retrieve_grader]", "yellow"), exc)
        if writer:
            writer({"node": "retrieve_grader", "message": "检索充分性判定失败，直接进入分析"})
        return {
            "retrieval_grade": {
                "sufficient": None, "gaps": [], "action": "continue",
                "round": round_index, "degraded": True,
            },
            "retrieval_round": 0,
            "retrieval_queries": [],
            "agent_messages": [],
        }

    # 重检需同时满足：判定不充分 + 有可用的补检词 + 未达上限。
    # 缺「补检词」这一条会退化成拿同样的词重复检索，白烧配额。
    should_retry = (not sufficient) and bool(rewritten) and round_index < max_rounds

    if should_retry:
        logger.info("%s 判定不充分，补检 %d 条 | round=%d→%d | gaps=%s",
                    colorize("[retrieve_grader]", "yellow"), len(rewritten),
                    round_index, round_index + 1, gaps)
        if writer:
            writer({"node": "retrieve_grader", "message": f"证据不足，补检 {len(rewritten)} 条后重试"})
        return {
            "retrieval_grade": {"sufficient": False, "gaps": gaps, "action": "retrieve", "round": round_index + 1},
            "retrieval_round": round_index + 1,
            "retrieval_queries": rewritten,
            "agent_messages": messages,
        }

    if sufficient:
        logger.info("%s 判定证据充分 | round=%d", colorize("[retrieve_grader]", "green"), round_index)
    else:
        reason = "已达重检上限" if round_index >= max_rounds else "裁判未给出可用检索词"
        logger.info("%s 判定不充分但不再重检（%s），继续分析 | round=%d",
                    colorize("[retrieve_grader]", "yellow"), reason, round_index)
        if writer:
            writer({"node": "retrieve_grader", "message": f"证据仍不足，但{reason}，继续分析"})

    # 归零 + 清空补检词：本轮检索阶段结束，下一轮外层研究从计划重新出发
    return {
        "retrieval_grade": {"sufficient": sufficient, "gaps": gaps, "action": "continue", "round": round_index},
        "retrieval_round": 0,
        "retrieval_queries": [],
        "agent_messages": messages,
    }
