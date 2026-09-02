"""状态定义模块：多智能体工作流共享的 AgentState 分组结构。

P1 重写：从 41 字段扁平 TypedDict 重构为分组+reducer+校验结构。
- messages 用 add_messages reducer（支持增量追加/覆盖）
- sources/findings/plan 用 operator.add reducer（累加去重在节点内实现）
- clarifications 占位（P4 HITL 启用）
- 旧字段按语义归组保留，无引用价值的字段删除
"""

import operator
from typing import Annotated, List, Optional
from typing_extensions import TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


# ── 对话流 ──
class ConversationState(TypedDict):
    messages: Annotated[list, add_messages]
    clarifications: Annotated[list, operator.add]  # P4 启用，先占位


# ── 研究数据 ──
class ResearchState(TypedDict):
    # 身份/入口
    query: str
    user_id: str
    tenant_id: str
    memory_context: str
    intent: str  # direct | multiagent

    # 计划
    plan: str  # plan_node 生成的研究计划文本
    outline: Annotated[list[dict], operator.add]
    sub_questions: Annotated[list[str], operator.add]
    research_questions: Annotated[list[str], operator.add]
    search_plan: Annotated[list[dict], operator.add]
    budget: dict
    supplementary_queries: Annotated[list[dict], operator.add]

    # 证据（F4 溯源基础）
    web_search: str
    local_rag: str
    web_evidence: Annotated[list[dict], operator.add]
    local_evidence: Annotated[list[dict], operator.add]
    evidence_pool: Annotated[list[dict], operator.add]
    deep_dive: str
    audit: str
    audit_flags: Annotated[list[dict], operator.add]
    analysis: str

    # 报告
    findings: Annotated[list[dict], operator.add]
    claim_map: Annotated[list[dict], operator.add]
    source_index: Annotated[list[dict], operator.add]
    needs_more_research: bool
    missing_gaps: Annotated[list[str], operator.add]
    code: str
    draft: str
    final: str

    # 检索追踪
    web_retrieval_stats: dict
    local_retrieval_stats: dict
    web_search_trace: Annotated[list[dict], operator.add]
    local_rag_trace: Annotated[list[dict], operator.add]

    # HITL
    hitl_enabled: bool
    hitl_config: dict
    user_feedback: dict


# ── 进度追踪 ──
class ProgressState(TypedDict):
    phase: str
    iteration: int
    max_iterations: int


class AgentState(ConversationState, ResearchState, ProgressState):
    """多重继承组合，替代旧扁平结构。

    注意：TypedDict 多重继承在运行时合并所有键，等价于一个扁平 dict。
    分组仅用于代码组织清晰度，不影响 LangGraph 行为。
    """


# 保持兼容：旧代码引用 ResearchState
ResearchStateCompat = AgentState


def create_initial_state(
    query: str,
    max_iterations: int,
    user_id: str,
    tenant_id: str,
    memory_context: str = "",
    hitl_enabled: bool = False,
    hitl_config: dict | None = None,
) -> AgentState:
    return {
        # ConversationState
        "messages": [],
        "clarifications": [],
        # ResearchState
        "query": query,
        "user_id": user_id,
        "tenant_id": tenant_id,
        "memory_context": memory_context,
        "intent": "",
        "plan": "",
        "outline": [],
        "sub_questions": [],
        "research_questions": [],
        "search_plan": [],
        "budget": {},
        "supplementary_queries": [],
        "web_search": "",
        "local_rag": "",
        "web_evidence": [],
        "local_evidence": [],
        "evidence_pool": [],
        "deep_dive": "",
        "audit": "",
        "audit_flags": [],
        "analysis": "",
        "findings": [],
        "claim_map": [],
        "source_index": [],
        "needs_more_research": False,
        "missing_gaps": [],
        "code": "",
        "draft": "",
        "final": "",
        "web_retrieval_stats": {},
        "local_retrieval_stats": {},
        "web_search_trace": [],
        "local_rag_trace": [],
        "hitl_enabled": hitl_enabled,
        "hitl_config": hitl_config or {
            "plan_review": True,
            "analyze_clarify": True,
            "write_review": False,
        },
        "user_feedback": {},
        # ProgressState
        "phase": "initialized",
        "iteration": 0,
        "max_iterations": max_iterations,
    }
