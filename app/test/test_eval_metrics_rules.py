"""规则型研报质量指标测试（`mult_agents/eval_metrics.py`）。

注意区分两个同名概念：
- 被测模块 `app/mult_agents/eval_metrics.py` —— 纯函数指标层，可离线单测
- 同目录脚本 `app/test/eval_metrics.py` —— 需要真实模型与中间件的 LLM-as-Judge 评测

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    python -m pytest app/test/test_eval_metrics_rules.py -v
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
sys.path.insert(0, str(_APP_PATH))

from mult_agents import eval_metrics  # noqa: E402


class TestEvidenceDuplicationRate:
    """reducer 语义哨兵：重复证据堆积必须能被量出来。"""

    def test_no_duplicates_reports_zero(self):
        state = {
            "web_evidence": [{"source_id": "W1"}, {"source_id": "W2"}],
            "local_evidence": [{"source_id": "L1"}],
            "evidence_pool": [{"source_id": "W1"}, {"source_id": "W2"}, {"source_id": "L1"}],
        }

        result = eval_metrics.evidence_duplication_rate(state)

        assert result["overall"] == 0.0
        assert result["total_items"] == 6
        assert result["unique_items"] == 6

    def test_detects_duplicated_evidence(self):
        """模拟 reducer 重复累加后的 State：同一来源出现两次。"""
        state = {
            "web_evidence": [{"source_id": "W1"}, {"source_id": "W1"}, {"source_id": "W2"}],
            "local_evidence": [],
            "evidence_pool": [],
        }

        result = eval_metrics.evidence_duplication_rate(state)

        assert result["per_channel"]["web_evidence"]["total"] == 3
        assert result["per_channel"]["web_evidence"]["unique"] == 2
        assert result["per_channel"]["web_evidence"]["duplication_rate"] == 0.3333
        assert result["overall"] > 0, "重复必须被量出来，否则哨兵失效"

    def test_empty_state_reports_zero(self):
        result = eval_metrics.evidence_duplication_rate({})

        assert result["overall"] == 0.0
        assert result["total_items"] == 0

    def test_ignores_items_without_source_id(self):
        state = {"web_evidence": [{"title": "无 id"}, {"source_id": "W1"}], "local_evidence": [], "evidence_pool": []}

        result = eval_metrics.evidence_duplication_rate(state)

        assert result["per_channel"]["web_evidence"]["total"] == 1


class TestRetrievalRounds:
    """检索深度：自适应检索生效后 queries_per_round 应上升。"""

    def test_groups_queries_by_round(self):
        state = {
            "iteration": 1,
            "web_search_trace": [
                {"iteration": 0, "query": "q1"},
                {"iteration": 0, "query": "q2"},
                {"iteration": 1, "query": "q3"},
            ],
            "local_rag_trace": [
                {"iteration": 0, "query": "q4"},
                {"iteration": 1, "query": "q5"},
            ],
        }

        result = eval_metrics.retrieval_rounds(state)

        assert result["outer_rounds"] == 2, "iteration=1 表示已跑完 2 轮（0 与 1）"
        assert result["total_queries"] == 5
        assert result["by_round"] == {"0": 3, "1": 2}
        assert result["queries_per_round"] == 2.5

    def test_empty_traces(self):
        result = eval_metrics.retrieval_rounds({"iteration": 0})

        assert result["outer_rounds"] == 1
        assert result["total_queries"] == 0
        assert result["queries_per_round"] == 0.0

    def test_skips_non_dict_traces(self):
        state = {"iteration": 0, "web_search_trace": [{"iteration": 0}, "垃圾"], "local_rag_trace": []}

        result = eval_metrics.retrieval_rounds(state)

        assert result["total_queries"] == 1


class TestCitationLegality:
    """引用角标必须能在来源表里找到，否则溯源断链。"""

    def test_all_legal(self):
        report = "结论一 [WEB1_1-1]，结论二 [LOC2_1-1]。"
        source_index = [{"source_id": "WEB1_1-1"}, {"source_id": "LOC2_1-1"}]

        result = eval_metrics.citation_legality(report, source_index)

        assert result["total"] == 2
        assert result["legal"] == 2
        assert result["legality_rate"] == 1.0
        assert result["illegal_ids"] == []

    def test_flags_illegal_citation(self):
        report = "结论一 [WEB1_1-1]，结论二 [WEB9_9-9]。"
        source_index = [{"source_id": "WEB1_1-1"}]

        result = eval_metrics.citation_legality(report, source_index)

        assert result["total"] == 2
        assert result["legal"] == 1
        assert result["legality_rate"] == 0.5
        assert result["illegal_ids"] == ["WEB9_9-9"], "非法角标必须能定位，便于排查断链"

    def test_deduplicates_repeated_citations(self):
        report = "甲 [WEB1_1-1]，乙 [WEB1_1-1]，丙 [WEB1_1-1]。"
        source_index = [{"source_id": "WEB1_1-1"}]

        result = eval_metrics.citation_legality(report, source_index)

        assert result["total"] == 1, "同一角标重复出现只计一次"

    def test_no_citations_reports_zero_rate(self):
        result = eval_metrics.citation_legality("无引用正文。", [{"source_id": "W1"}])

        assert result["total"] == 0
        assert result["legality_rate"] == 0.0

    def test_handles_empty_report(self):
        result = eval_metrics.citation_legality("", None)

        assert result["total"] == 0


class TestKeyPointCoverage:
    def test_matches_case_insensitively(self):
        report = "本文讨论 langgraph 与 vLLM 的配合。"

        result = eval_metrics.key_point_coverage(report, ["LangGraph", "vLLM", "Milvus"])

        assert result["hit"] == 2
        assert result["coverage"] == 0.6667
        assert result["missed"] == ["Milvus"]

    def test_empty_points(self):
        result = eval_metrics.key_point_coverage("正文", [])

        assert result["coverage"] == 0.0
        assert result["total"] == 0


class TestAggregate:
    def test_averages_field_across_entries(self):
        per_query = [
            {"evidence_duplication_rate": {"overall": 0.0}},
            {"evidence_duplication_rate": {"overall": 0.4}},
        ]

        assert eval_metrics.aggregate(per_query, "evidence_duplication_rate", "overall") == 0.2

    def test_skips_missing_entries(self):
        per_query = [
            {"retrieval_rounds": {"queries_per_round": 3.0}},
            {},
            {"retrieval_rounds": {"queries_per_round": 5.0}},
        ]

        assert eval_metrics.aggregate(per_query, "retrieval_rounds", "queries_per_round") == 4.0

    def test_empty_returns_zero(self):
        assert eval_metrics.aggregate([], "x", "y") == 0.0
        assert eval_metrics.aggregate(None, "x", "y") == 0.0


class TestMeasure:
    def test_returns_all_metric_groups(self):
        state = {
            "web_evidence": [{"source_id": "WEB1_1-1"}],
            "local_evidence": [],
            "evidence_pool": [],
            "source_index": [{"source_id": "WEB1_1-1"}],
            "iteration": 0,
            "web_search_trace": [{"iteration": 0}],
            "local_rag_trace": [],
        }

        result = eval_metrics.measure(state, "结论 [WEB1_1-1]。", ["结论"])

        assert set(result) == {
            "evidence_duplication_rate",
            "retrieval_rounds",
            "citation_legality",
            "key_point_coverage",
        }
        assert result["citation_legality"]["legality_rate"] == 1.0
        assert result["key_point_coverage"]["coverage"] == 1.0


class TestTokenAccumulator:
    """token 统计回归。

    旧实现把钩子挂在 agent 对象上（`agent._generate`），而结构化节点是
    `StructuredAgent`（内含 create_agent 编译出的图）——**没有 `_generate`**，
    于是挂载条件恒为假、一次都没挂上，token 统计恒为 0 且不报错，
    报告里的「Token 消耗降低比例」是假数据。改为 LangChain 回调后在此锁住。
    """

    @staticmethod
    def _script():
        import importlib.util

        script_path = Path(__file__).resolve().parent / "eval_metrics.py"
        spec = importlib.util.spec_from_file_location("eval_metrics_script", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _result(usage: dict | None = None):
        from langchain_core.outputs import Generation, LLMResult

        return LLMResult(
            generations=[[Generation(text="x")]],
            llm_output={"token_usage": usage} if usage else {},
        )

    def test_accumulates_tokens_from_llm_output(self):
        acc = self._script().TokenAccumulator()

        acc.on_llm_end(self._result({"prompt_tokens": 10, "completion_tokens": 5}))
        acc.on_llm_end(self._result({"prompt_tokens": 1, "completion_tokens": 2}))

        assert acc.total_prompt_tokens == 11
        assert acc.total_completion_tokens == 7
        assert acc.total_tokens == 18
        assert acc.call_count == 2

    def test_falls_back_to_usage_metadata(self):
        """llm_output 无 usage 时回落到 message.usage_metadata。"""
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, LLMResult

        acc = self._script().TokenAccumulator()
        message = AIMessage(
            content="x",
            usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
        )

        acc.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message)]], llm_output={}))

        assert acc.total_tokens == 10

    def test_unparsed_usage_is_counted_and_warned(self, caplog):
        """解析不到 usage 必须留痕——否则又回到「静默归零」。"""
        acc = self._script().TokenAccumulator()

        acc.on_llm_end(self._result())

        assert acc.unparsed_calls == 1
        assert acc.call_count == 0
        with caplog.at_level("WARNING"):
            acc.warn_if_unparsed()
        assert "token 统计未取到任何 usage" in caplog.text

    def test_no_warning_when_usage_parsed(self, caplog):
        acc = self._script().TokenAccumulator()

        acc.on_llm_end(self._result({"prompt_tokens": 1, "completion_tokens": 1}))
        with caplog.at_level("WARNING"):
            acc.warn_if_unparsed()

        assert "token 统计未取到任何 usage" not in caplog.text

    def test_reset_clears_counters(self):
        acc = self._script().TokenAccumulator()

        acc.on_llm_end(self._result({"prompt_tokens": 1, "completion_tokens": 1}))
        acc.reset()

        assert acc.total_tokens == 0
        assert acc.call_count == 0
        assert acc.unparsed_calls == 0

    def test_is_a_langchain_callback(self):
        """必须是 BaseCallbackHandler，才能通过 invoke config 的 callbacks 注入。"""
        from langchain_core.callbacks import BaseCallbackHandler

        assert isinstance(self._script().TokenAccumulator(), BaseCallbackHandler)
