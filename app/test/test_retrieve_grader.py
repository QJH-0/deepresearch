"""检索充分性裁判节点测试（B1 内层自适应循环）。

覆盖的决策分支（每个都是一条防死循环或防静默降级的规则）：

1. 判定充分 → 放行，且轮次归零
2. 判定不充分 + 有补检词 + 未达上限 → 回检索重试
3. 判定不充分但**已达上限** → 放行（否则死循环）
4. 判定不充分但**没给补检词** → 放行（否则拿同样的词空转，白烧配额）
5. 结构化失败 → 放行且 `degraded=True` 留痕（否则自适应能力被静默关闭）

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    python -m pytest app/test/test_retrieve_grader.py -v
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
sys.path.insert(0, str(_APP_PATH))

from mult_agents.nodes import retrieve_grader as grader_module  # noqa: E402
from mult_agents.output_schemas import RetrievalGradeDraft, SupplementaryQuery  # noqa: E402


def _state(**overrides) -> dict:
    state = {
        "query": "AI Agent 框架发展趋势",
        "sub_questions": ["架构演进", "生态格局"],
        "search_plan": [
            {"query": "首轮检索词", "source_preference": "hybrid", "section_id": "s1", "reason": ""}
        ],
        "retrieval_queries": [],
        "retrieval_round": 0,
        "max_retrieval_rounds": 2,
        "web_evidence": [{"source_id": "WEB1_1-1", "snippet": "证据", "relevance_score": 0.8}],
        "local_evidence": [],
        "evidence_pool": [],
    }
    state.update(overrides)
    return state


def _query(text: str) -> SupplementaryQuery:
    return SupplementaryQuery(section_id="gap_1", query=text, reason="补缺口")


def _patch(monkeypatch, draft=None, error=None):
    async def fake_invoke(state, prompt, agent, agent_name, node, writer=None):
        if error is not None:
            raise error
        return draft, []

    monkeypatch.setattr(grader_module, "_invoke_structured_agent", fake_invoke)


class TestRetrievalGraderNode:
    async def test_sufficient_continues_and_resets_round(self, monkeypatch):
        _patch(monkeypatch, RetrievalGradeDraft(sufficient=True))

        out = await grader_module.retrieval_grader_node(
            _state(retrieval_round=1), None, "retrieval_grader"
        )

        assert out["retrieval_grade"]["action"] == "continue"
        assert out["retrieval_round"] == 0, "出检索阶段必须归零，供下一轮外层研究复用"
        assert out["retrieval_queries"] == [], "补检词必须清空，否则会污染下一轮"

    async def test_insufficient_with_queries_retries(self, monkeypatch):
        draft = RetrievalGradeDraft(
            sufficient=False, gaps=["缺生态数据"], rewritten_queries=[_query("补检词A")]
        )
        _patch(monkeypatch, draft)

        out = await grader_module.retrieval_grader_node(_state(), None, "retrieval_grader")

        assert out["retrieval_grade"]["action"] == "retrieve"
        assert out["retrieval_round"] == 1
        assert [item["query"] for item in out["retrieval_queries"]] == ["补检词A"]

    async def test_stops_at_max_rounds(self, monkeypatch):
        """已达上限必须放行——这是防死循环的硬闸。"""
        draft = RetrievalGradeDraft(sufficient=False, rewritten_queries=[_query("补检词A")])
        _patch(monkeypatch, draft)

        out = await grader_module.retrieval_grader_node(
            _state(retrieval_round=2, max_retrieval_rounds=2), None, "retrieval_grader"
        )

        assert out["retrieval_grade"]["action"] == "continue"
        assert out["retrieval_round"] == 0

    async def test_insufficient_without_queries_does_not_retry(self, monkeypatch):
        """裁判说不足但没给词——重检只会拿同样的词空转，必须放行。"""
        _patch(monkeypatch, RetrievalGradeDraft(sufficient=False, gaps=["缺数据"]))

        out = await grader_module.retrieval_grader_node(_state(), None, "retrieval_grader")

        assert out["retrieval_grade"]["action"] == "continue"
        assert out["retrieval_grade"]["sufficient"] is False, "仍要如实记录判定，便于观测"

    async def test_structured_failure_degrades_and_continues(self, monkeypatch):
        """裁判失败要放行，但必须留痕——否则自适应能力被静默关闭。"""
        from mult_agents.nodes._parsing import StructuredOutputError

        _patch(monkeypatch, error=StructuredOutputError("裁判挂了"))

        out = await grader_module.retrieval_grader_node(_state(), None, "retrieval_grader")

        assert out["retrieval_grade"]["action"] == "continue"
        assert out["retrieval_grade"]["degraded"] is True
        assert out["retrieval_round"] == 0

    async def test_uses_grader_queries_as_executed_list_on_retry(self, monkeypatch):
        """重检轮次应把「上一轮补检词」当作已执行检索词交给裁判，避免重复出词。"""
        captured = {}

        async def fake_invoke(state, prompt, agent, agent_name, node, writer=None):
            captured["prompt"] = prompt
            return RetrievalGradeDraft(sufficient=True), []

        monkeypatch.setattr(grader_module, "_invoke_structured_agent", fake_invoke)
        state = _state(retrieval_queries=[{"query": "上一轮补检词", "section_id": "gap_1"}])

        await grader_module.retrieval_grader_node(state, None, "retrieval_grader")

        assert "上一轮补检词" in captured["prompt"]
        assert "首轮检索词" not in captured["prompt"], "重检轮次不应再报首轮计划为已执行词"


class TestRetrievalPassIndex:
    """source_id 的批次号必须在内层重检时也不撞车。

    实测教训：B1 之前用 `iteration` 生成前缀，引入内层重检后同一外层轮次内
    多次检索得到相同前缀 → `WEB1_1-1` 同时指向两条不同证据，
    证据重复率虚高到 41%，引用溯源断链。
    """

    def test_unique_across_outer_and_inner_rounds(self):
        from mult_agents.nodes._evidence import _retrieval_pass_index

        seen = [
            _retrieval_pass_index(
                {"iteration": outer, "retrieval_round": inner, "max_retrieval_rounds": 2}
            )
            for outer in range(3)
            for inner in range(3)
        ]

        assert len(seen) == len(set(seen)), f"批次号必须两两不同，实际 {seen}"

    def test_starts_at_one(self):
        from mult_agents.nodes._evidence import _retrieval_pass_index

        assert _retrieval_pass_index({}) == 1

    def test_stays_pure_digits_so_id_format_holds(self):
        """source_id 的格式约束是 [A-Z]+\\d+_\\d+-\\d+，批次号掺字母会让正则失配。"""
        import re

        from mult_agents.nodes._evidence import _retrieval_pass_index

        for outer in range(3):
            for inner in range(3):
                index = _retrieval_pass_index(
                    {"iteration": outer, "retrieval_round": inner, "max_retrieval_rounds": 2}
                )
                assert re.fullmatch(r"\d+", str(index)), f"批次号 {index} 不是纯数字"


class TestRecursionLimit:
    """内层重检放大超步数：默认 recursion_limit=25 不够。

    实测 `max_iterations=2` 时抛
    `Recursion limit of 25 reached without hitting a stop condition`，整轮研究失败。
    """

    def test_exceeds_langgraph_default(self):
        from mult_agents.runtime import recursion_limit_for

        assert recursion_limit_for(2, 2) > 25, "实测该配置会撞破默认上限 25"

    def test_scales_with_both_limits(self):
        from mult_agents.runtime import recursion_limit_for

        assert recursion_limit_for(3, 2) > recursion_limit_for(2, 2), "外层轮次越多，上限越高"
        assert recursion_limit_for(2, 3) > recursion_limit_for(2, 2), "内层重检越多，上限越高"


class TestRouteAfterRetrievalGrade:
    """路由函数只做路由决策，判定结果由节点写入。

    返回的是**节点名**：LangGraph 的 path_map 只接受节点名列表，
    扇出到两条检索边靠返回列表实现。
    """

    def test_retrieve_action_fans_out_to_both_scouts(self):
        from mult_agents.graph import route_after_retrieval_grade

        assert route_after_retrieval_grade({"retrieval_grade": {"action": "retrieve"}}) == [
            "web_search", "local_rag",
        ]

    def test_continue_action_goes_to_deep_dive(self):
        from mult_agents.graph import route_after_retrieval_grade

        assert route_after_retrieval_grade({"retrieval_grade": {"action": "continue"}}) == "deep_dive"

    def test_defaults_to_deep_dive_when_missing(self):
        """缺字段时不得卡在检索阶段——默认放行。"""
        from mult_agents.graph import route_after_retrieval_grade

        assert route_after_retrieval_grade({}) == "deep_dive"
        assert route_after_retrieval_grade({"retrieval_grade": {}}) == "deep_dive"
