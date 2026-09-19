"""工作流编排模块：定义 LangGraph 节点、条件路由与整体执行路径。

拓扑：
    START → intent →(direct_answer | clarify → plan)
    plan →(web_search ∥ local_rag) → retrieve_grader
    retrieve_grader →(不充分且未达上限)→ 回 (web_search ∥ local_rag)
    retrieve_grader →(充分 / 达上限)→ deep_dive → analyze
    analyze →(需继续研究且未达迭代上限)→ 回 (web_search ∥ local_rag)
    analyze →(证据充分 / 达上限)→ write → END

两级循环：`retrieve_grader` 管**检索阶段内部**的自适应重检，
`analyze` 的 `next_action` 管**跨轮次**的继续研究。层级不同，别混用。
"""


import logging

from langgraph.graph import StateGraph, START, END

from .nodes import (
    bind_agent,
    intent_node,
    direct_answer_node,
    plan_node,
    web_search_node,
    local_rag_node,
    retrieval_grader_node,
    deep_dive_node,
    analyze_node,
    write_node,
    clarify_node,
)
from .state import AgentState


logger = logging.getLogger("mult_agents")


def route_after_intent(state: AgentState) -> str:
    """direct → direct_answer; 其他 → clarify（P4 加 interrupt）→ plan"""
    if state.get("intent") == "direct":
        return "direct_answer"
    return "clarify"


def route_after_analyze(state: AgentState) -> str | list[str]:
    """只做路由决策：`next_action` 由 analyze 节点写入（含迭代上限的判定）。

    返回**节点名**：LangGraph 的 path_map 只接受节点名列表。
    继续研究时返回列表，扇出回两条检索边。
    """
    if state.get("next_action") == "reflect":
        return ["web_search", "local_rag"]
    return "write"


def route_after_retrieval_grade(state: AgentState) -> str | list[str]:
    """只做路由决策：判定结果由 grader 节点写入 `retrieval_grade.action`。

    返回**节点名**而非语义标签：LangGraph 的 path_map 只接受节点名列表，
    不支持「一个标签映射到多个节点」。扇出到两条检索边要靠返回列表实现。
    """
    if state.get("retrieval_grade", {}).get("action") == "retrieve":
        return ["web_search", "local_rag"]
    return "deep_dive"


def build_app(agents, checkpointer):
    workflow = StateGraph(AgentState)
    workflow.add_node("intent", bind_agent(intent_node, agents.intent_router, "intent_router"))
    workflow.add_node("direct_answer", bind_agent(direct_answer_node, agents.direct_responder, "direct_responder"))
    workflow.add_node("clarify", bind_agent(clarify_node, agents.clarifier, "clarifier"))
    workflow.add_node("plan", bind_agent(plan_node, agents.planner, "planner"))
    workflow.add_node("web_search", bind_agent(web_search_node, agents.scout_web, "scout_web"))
    workflow.add_node("local_rag", bind_agent(local_rag_node, agents.scout_local, "scout_local"))
    workflow.add_node(
        "retrieve_grader",
        bind_agent(retrieval_grader_node, agents.retrieval_grader, "retrieval_grader"),
    )
    workflow.add_node("deep_dive", bind_agent(deep_dive_node, agents.evidence_judge, "evidence_judge"))
    workflow.add_node("analyze", bind_agent(analyze_node, agents.analyst, "analyst"))
    workflow.add_node("write", bind_agent(write_node, agents.writer, "writer"))

    workflow.add_edge(START, "intent")
    workflow.add_conditional_edges(
        "intent",
        route_after_intent,
        {
            "direct_answer": "direct_answer",
            "clarify": "clarify",
        },
    )
    # clarify 直通 plan（P4 加 interrupt）
    workflow.add_edge("clarify", "plan")
    workflow.add_edge("plan", "web_search")
    workflow.add_edge("plan", "local_rag")
    # 两条检索边汇入同一个裁判节点：LangGraph 等两条入边都到达才执行，
    # 因此 grader 看到的是 web + local 合并后的证据
    workflow.add_edge("web_search", "retrieve_grader")
    workflow.add_edge("local_rag", "retrieve_grader")
    workflow.add_conditional_edges(
        "retrieve_grader",
        route_after_retrieval_grade,
        # path_map 只能是节点名列表；扇出到两条检索边由路由函数返回列表实现
        ["web_search", "local_rag", "deep_dive"],
    )
    workflow.add_edge("deep_dive", "analyze")

    # 继续研究的判定收在 analyze 节点内（含迭代上限），路由函数只读 next_action
    workflow.add_conditional_edges(
        "analyze",
        route_after_analyze,
        ["web_search", "local_rag", "write"],
    )

    workflow.add_edge("direct_answer", END)
    workflow.add_edge("write", END)

    return workflow.compile(checkpointer=checkpointer)
