"""状态定义模块：多智能体工作流共享的 AgentState 分组结构。

**reducer 的选用规则（唯一判据）**：看字段语义是「历史累积」还是「当前值」。

- **历史累积** → 用 `operator.add`，且**节点只能返回本轮增量**。
  节点若返回全量（`existing + new`），reducer 会再拼一次，导致每轮近似翻倍
  （递推 `e(n+1) = 2·e(n) + new`）。适用于：证据库、检索轨迹、澄清问答。
- **当前值** → 不加 reducer，节点返回全量，后写覆盖前写。
  适用于：计划（outline/sub_questions/search_plan）、每轮重建的派生数据
  （evidence_pool/source_index/audit_flags）、每轮重新分析得出的结论
  （findings/claim_map/missing_gaps）。

语义回归由 `app/test/test_state_semantics.py` 锁定，改动 reducer 前先看该文件。
"""

import operator
from typing import Annotated, List, Optional
from typing_extensions import TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


# ── 对话流 ──
class ConversationState(TypedDict):
    chat_messages: Annotated[list, add_messages]       # 用户可见对话（前端读取）
    agent_messages: Annotated[list, add_messages]      # agent 内部上下文（前端不可见）
    clarifications: Annotated[list, operator.add]      # P4 启用，先占位
    conversation_summary: str                          # 对话摘要文本（消息超阈值时 LLM 压缩生成）


# ── 研究数据 ──
class ResearchState(TypedDict):
    # 身份/入口
    query: str
    user_id: str
    tenant_id: str
    memory_context: str
    intent: str  # direct | multiagent

    # 计划（当前值：plan 节点每轮重新生成，覆盖旧计划）
    plan: str  # plan_node 生成的研究计划文本
    outline: list[dict]
    sub_questions: list[str]
    research_questions: list[str]
    search_plan: list[dict]
    budget: dict
    supplementary_queries: list[dict]

    # 证据（F4 溯源基础）
    # web_evidence / local_evidence 是跨轮累积的证据库：由 reducer 负责拼接，
    # 检索节点只能返回本轮增量
    web_evidence: Annotated[list[dict], operator.add]
    local_evidence: Annotated[list[dict], operator.add]
    # evidence_pool / audit_flags 由 deep_dive 每轮从全量证据重建，属当前值
    evidence_pool: list[dict]
    audit_flags: list[dict]
    analysis: str

    # 报告（当前值：analyze 每轮重新分析得出，覆盖旧结论）
    findings: list[dict]
    claim_map: list[dict]
    source_index: list[dict]
    needs_more_research: bool
    missing_gaps: list[str]
    # 研究笔记（累积：每轮一条，由 analyze 从 findings/gaps/审计标记汇编）
    # 作用：让后续轮次与报告附录读「结论演进」，而不必重读全部证据
    research_notes: Annotated[list[dict], operator.add]
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
    plan_revision_count: int  # P4: plan revise 轮次计数（防死循环上限 3）
    clarify_rounds: int  # R2.3: LLM 澄清轮次计数（上限 2，防死循环）


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
        "chat_messages": [],
        "agent_messages": [],
        "clarifications": [],
        "conversation_summary": "",
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
        "web_evidence": [],
        "local_evidence": [],
        "evidence_pool": [],
        "audit_flags": [],
        "analysis": "",
        "findings": [],
        "claim_map": [],
        "source_index": [],
        "needs_more_research": False,
        "missing_gaps": [],
        "research_notes": [],
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
        "plan_revision_count": 0,
        "clarify_rounds": 0,
        # ProgressState
        "phase": "initialized",
        "iteration": 0,
        "max_iterations": max_iterations,
    }
