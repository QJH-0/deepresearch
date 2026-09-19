"""State reducer 语义与节点返回契约测试。

背景：state.py 曾对 15 个字段统一声明 `operator.add`，但节点的返回约定不统一，
两类错误都会让 State 膨胀：

1. **累积型字段的节点返回了全量**（如 web_search 返回 `existing + evidence`），
   reducer 再拼一次 → 每轮迭代证据列表近似翻倍（递推 `e(n+1) = 2·e(n) + new`）。
2. **覆盖型字段被声明成累积**（如 outline / evidence_pool 每轮重建），
   旧值不会被丢弃 → 计划与证据池随迭代轮次线性堆积。

本文件用**真实节点函数**与**真实 StateGraph** 锁定语义，替代原先只断言源码
字符串的弱检查（`test_state.py::test_sources_reducer_uses_operator_add`）。

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    python -m pytest app/test/test_state_semantics.py -v
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
sys.path.insert(0, str(_APP_PATH))

from mult_agents.state import AgentState  # noqa: E402


# 累积型字段：语义是「历史证据库」，跨轮累加
ACCUMULATING_CHANNELS = (
    "web_evidence",
    "local_evidence",
    "web_search_trace",
    "local_rag_trace",
    "clarifications",
    "research_notes",
)

# 覆盖型字段：语义是「当前值」，每轮重建
CURRENT_VALUE_CHANNELS = (
    "outline",
    "sub_questions",
    "research_questions",
    "search_plan",
    "supplementary_queries",
    "evidence_pool",
    "source_index",
    "audit_flags",
    "findings",
    "claim_map",
    "missing_gaps",
    "retrieval_grade",
    "retrieval_queries",
)


# ──────────────────────────────────────────────────────────────
# 第一组：节点返回契约（真实节点函数）
# ──────────────────────────────────────────────────────────────

EXISTING_WEB_EVIDENCE = [
    {"source_id": "WEB0_1-1", "title": "上一轮证据甲", "url": "https://old.com/1",
     "snippet": "旧", "source_type": "web"},
    {"source_id": "WEB0_1-2", "title": "上一轮证据乙", "url": "https://old.com/2",
     "snippet": "旧", "source_type": "web"},
]

EXISTING_LOCAL_EVIDENCE = [
    {"source_id": "LOC0_1-1", "title": "上一轮本地文档", "doc_id": "doc-1",
     "snippet": "旧", "source_type": "local"},
]


def _web_state_with_existing_evidence(query="LangGraph 编排"):
    """构造「已有一轮证据」的 State，模拟第二轮检索。

    注意 iteration=1 与 web_search_trace 非空，是为了让节点走「回环轮次」而非首轮路径。
    """
    return {
        "query": query,
        "search_plan": [
            {"query": query, "source_preference": "web", "section_id": "sec_1", "reason": "t"}
        ],
        "web_evidence": [dict(item) for item in EXISTING_WEB_EVIDENCE],
        "web_search_trace": [
            {"iteration": 0, "query": "上一轮查询", "raw_count": 2, "raw_records": []}
        ],
        "web_retrieval_stats": {},
        "sub_questions": [],
        "iteration": 1,
    }


def _local_state_with_existing_evidence(query="LangGraph 编排"):
    return {
        "query": query,
        "search_plan": [
            {"query": query, "source_preference": "local", "section_id": "sec_1", "reason": "t"}
        ],
        "local_evidence": [dict(item) for item in EXISTING_LOCAL_EVIDENCE],
        "local_rag_trace": [
            {"iteration": 0, "query": "上一轮查询", "raw_count": 1, "raw_records": []}
        ],
        "local_retrieval_stats": {},
        "sub_questions": [],
        "iteration": 1,
    }


class TestWebSearchNodeReturnContract:
    """web_search 节点只返回本轮增量，由 reducer 负责累加。"""

    async def test_returns_only_new_evidence(self, monkeypatch):
        """已有 2 条证据时，节点只能返回本轮新增的那 1 条。"""
        from mult_agents.nodes import web_search
        from mult_agents.nodes._parsing import StructuredOutputError

        def fake_search(query, count=4):
            return [{"title": "LangGraph 编排指南", "url": "https://new.com/1",
                     "snippet": "LangGraph 是编排框架", "domain": "new.com"}]

        async def fake_structured(state, prompt, agent, agent_name, node, writer=None):
            raise StructuredOutputError("走降级分支，不调用模型")

        monkeypatch.setattr(web_search, "web_search_records", fake_search)
        monkeypatch.setattr(web_search, "_invoke_structured_agent", fake_structured)

        out = await web_search.web_search_node(
            _web_state_with_existing_evidence(), None, "scout_web"
        )

        returned = out["web_evidence"]
        assert len(returned) == 1, (
            f"节点应只返回本轮新增证据，实际返回 {len(returned)} 条；"
            f"若为 3 条说明节点回写了旧值，reducer 会把旧证据再翻一倍"
        )
        old_ids = {item["source_id"] for item in EXISTING_WEB_EVIDENCE}
        assert old_ids.isdisjoint({item["source_id"] for item in returned}), (
            "节点返回值中不得包含上一轮已有的证据"
        )

    async def test_omits_evidence_key_when_no_records(self, monkeypatch):
        """无新证据时必须省略 web_evidence 键。

        返回 `state.get("web_evidence", [])` 会让 reducer 执行 `旧值 + 旧值`，
        即「没搜到任何东西」反而把证据翻倍——这是最隐蔽的一处放大。
        """
        from mult_agents.nodes import web_search

        def fake_search(query, count=4):
            return []

        monkeypatch.setattr(web_search, "web_search_records", fake_search)

        out = await web_search.web_search_node(
            _web_state_with_existing_evidence(), None, "scout_web"
        )

        assert "web_evidence" not in out, (
            "无新证据时不得回写 web_evidence，否则 reducer 会把已有证据翻倍"
        )

    async def test_returns_only_current_round_traces(self, monkeypatch):
        """已有 1 条轨迹时，节点只能返回本轮新轨迹。"""
        from mult_agents.nodes import web_search
        from mult_agents.nodes._parsing import StructuredOutputError

        def fake_search(query, count=4):
            return [{"title": "LangGraph 编排指南", "url": "https://new.com/1",
                     "snippet": "LangGraph 是编排框架", "domain": "new.com"}]

        async def fake_structured(state, prompt, agent, agent_name, node, writer=None):
            raise StructuredOutputError("走降级分支，不调用模型")

        monkeypatch.setattr(web_search, "web_search_records", fake_search)
        monkeypatch.setattr(web_search, "_invoke_structured_agent", fake_structured)

        out = await web_search.web_search_node(
            _web_state_with_existing_evidence(), None, "scout_web"
        )

        assert len(out["web_search_trace"]) == 1, (
            f"应只返回本轮轨迹，实际 {len(out['web_search_trace'])} 条"
        )


class TestLocalRagNodeReturnContract:
    """local_rag 节点只返回本轮增量。"""

    async def test_returns_only_new_evidence(self, monkeypatch):
        from mult_agents.nodes import local_rag
        from mult_agents.nodes._parsing import StructuredOutputError

        def fake_search(query, count=4):
            return [{"title": "本地编排文档", "doc_id": "doc-9",
                     "snippet": "LangGraph 编排说明", "domain": "local"}]

        async def fake_structured(state, prompt, agent, agent_name, node, writer=None):
            raise StructuredOutputError("走降级分支，不调用模型")

        monkeypatch.setattr(local_rag, "search_knowledge_base_records", fake_search)
        monkeypatch.setattr(local_rag, "_invoke_structured_agent", fake_structured)

        out = await local_rag.local_rag_node(
            _local_state_with_existing_evidence(), None, "scout_local"
        )

        returned = out["local_evidence"]
        assert len(returned) == 1, (
            f"节点应只返回本轮新增证据，实际返回 {len(returned)} 条"
        )
        old_ids = {item["source_id"] for item in EXISTING_LOCAL_EVIDENCE}
        assert old_ids.isdisjoint({item["source_id"] for item in returned})

    async def test_omits_evidence_key_when_no_records(self, monkeypatch):
        from mult_agents.nodes import local_rag

        def fake_search(query, count=4):
            return []

        monkeypatch.setattr(local_rag, "search_knowledge_base_records", fake_search)

        out = await local_rag.local_rag_node(
            _local_state_with_existing_evidence(), None, "scout_local"
        )

        assert "local_evidence" not in out


# ──────────────────────────────────────────────────────────────
# 第二组：Schema 语义（真实 StateGraph）
# ──────────────────────────────────────────────────────────────


def _run_two_step_graph(channel: str, first_value, second_value, seed):
    """在真实 AgentState 上跑两步图，返回该 channel 的最终值。

    两步串联而非单节点自环：既验证 reducer 是否生效，也验证第二步看到的是
    第一步之后的累积结果（自环需要额外计数条件，此处不必要）。
    """
    from langgraph.graph import StateGraph, START, END

    def first(state):
        return {channel: first_value}

    def second(state):
        return {channel: second_value}

    builder = StateGraph(AgentState)
    builder.add_node("first", first)
    builder.add_node("second", second)
    builder.add_edge(START, "first")
    builder.add_edge("first", "second")
    builder.add_edge("second", END)
    graph = builder.compile()

    return graph.invoke({channel: seed})[channel]


class TestChannelSemantics:
    """累积型字段跨步追加，覆盖型字段只留最后一步的值。"""

    def test_accumulating_channels_append(self):
        for channel in ACCUMULATING_CHANNELS:
            result = _run_two_step_graph(
                channel, ["A"], ["B"], seed=["seed"]
            )
            assert result == ["seed", "A", "B"], (
                f"{channel} 应为累积语义，实际 {result}"
            )

    def test_current_value_channels_overwrite(self):
        for channel in CURRENT_VALUE_CHANNELS:
            result = _run_two_step_graph(
                channel, ["A"], ["B"], seed=["seed"]
            )
            assert result == ["B"], (
                f"{channel} 应为覆盖语义（当前值），实际 {result}；"
                f"若出现旧值说明仍声明了 operator.add reducer"
            )
