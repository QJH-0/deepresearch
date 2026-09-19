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


class TestCitationCoverage:
    """引用覆盖率：少数天然有判别力的指标，不依赖人工标注。"""

    def test_counts_only_substantive_sentences(self):
        """短句（标题、过渡语、孤立角标）不计入分母——它们本就不该被要求带角标。"""
        report = "短句。[WEB1_1-1]。这是一句足够长的主要论断，但它没有带任何引用角标。"

        result = eval_metrics.citation_coverage(report)

        assert result["total"] == 1, "过短的句子与孤立角标都不应计入主要论断"
        assert result["cited"] == 0
        assert result["coverage"] == 0.0

    def test_coverage_is_cited_over_total(self):
        report = (
            "第一句足够长的主要论断，带角标 [WEB1_1-1]。"
            "第二句同样足够长，也带角标 [LOC2_1-1]。"
            "第三句同样足够长，但完全没有角标支撑。"
            "第四句同样足够长，同样没有任何角标。"
        )

        result = eval_metrics.citation_coverage(report)

        assert result["total"] == 4
        assert result["cited"] == 2
        assert result["coverage"] == 0.5

    def test_empty_report(self):
        result = eval_metrics.citation_coverage("")

        assert result["total"] == 0
        assert result["coverage"] == 0.0

    def test_matches_write_node_embedding(self):
        """与 write 节点 P7-2 埋点同源：节点调用本函数，不再各写一份。"""
        from mult_agents.eval_metrics import citation_coverage as metric
        from mult_agents.nodes import write as write_module

        assert write_module.citation_coverage is metric


class TestExpectedSourceRecall:
    """期望来源召回：需人工标注，未标注的题必须跳过而不是算 0。"""

    def test_not_applicable_when_no_expectations(self):
        result = eval_metrics.expected_source_recall(
            "正文 [WEB1_1-1]。", [{"source_id": "WEB1_1-1", "locator": "https://a.com"}], []
        )

        assert result["applicable"] is False
        assert result["recall"] is None, (
            "未标注必须返回 None，聚合才会跳过；返回 0.0 会把「没标注」误算成「没召回」"
        )

    def test_counts_hit_by_locator_fragment(self):
        report = "结论 [WEB1_1-1] 与 [LOC2_1-1]。"
        index = [
            {"source_id": "WEB1_1-1", "locator": "https://arxiv.org/abs/2501.00001"},
            {"source_id": "LOC2_1-1", "locator": "/kb/whitepaper.md"},
        ]

        result = eval_metrics.expected_source_recall(report, index, ["arxiv.org", "openai.com"])

        assert result["applicable"] is True
        assert result["hit"] == 1
        assert result["recall"] == 0.5
        assert result["missed"] == ["openai.com"]

    def test_ignores_sources_that_were_not_cited(self):
        """来源表里有 arxiv，但正文没引用它——不算召回。"""
        report = "结论 [LOC2_1-1]。"
        index = [
            {"source_id": "WEB1_1-1", "locator": "https://arxiv.org/abs/2501.00001"},
            {"source_id": "LOC2_1-1", "locator": "/kb/whitepaper.md"},
        ]

        result = eval_metrics.expected_source_recall(report, index, ["arxiv.org"])

        assert result["hit"] == 0, "只统计被正文实际引用的来源"

    def test_matching_is_case_insensitive(self):
        result = eval_metrics.expected_source_recall(
            "结论 [WEB1_1-1]。",
            [{"source_id": "WEB1_1-1", "locator": "https://ARXIV.org/abs/1"}],
            ["arXiv.org"],
        )

        assert result["hit"] == 1

    def test_aggregate_skips_not_applicable_entries(self):
        per_query = [
            {"expected_source_recall": {"recall": None}},
            {"expected_source_recall": {"recall": 0.5}},
            {"expected_source_recall": {"recall": 1.0}},
        ]

        assert eval_metrics.aggregate(per_query, "expected_source_recall", "recall") == 0.75


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

        result = eval_metrics.measure(state, "结论：这是一句足够长的主要论断，带上了引用角标 [WEB1_1-1]。", ["结论"])

        assert set(result) == {
            "evidence_duplication_rate",
            "retrieval_rounds",
            "citation_legality",
            "citation_coverage",
            "key_point_coverage",
            "expected_source_recall",
        }
        assert result["citation_legality"]["legality_rate"] == 1.0
        assert result["citation_coverage"]["coverage"] == 1.0
        assert result["key_point_coverage"]["coverage"] == 1.0
        assert result["expected_source_recall"]["applicable"] is False


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


class TestRetrievalHealth:
    """web 检索健康度：某阶段一条 web 证据都没拿到时，阶段对照无效。

    实测教训：本轮评测 baseline 的 20 次 web 检索全部超时（DuckDuckGo 38 次超时、
    startpage 连接被拒），拿到 0 条 web 证据；而 improved 拿到 18 条。
    这种污染如果不显式标出来，指标差异极易被误读成「代码改动有害」。
    """

    @staticmethod
    def _script():
        import importlib.util

        script_path = Path(__file__).resolve().parent / "eval_metrics.py"
        spec = importlib.util.spec_from_file_location("eval_metrics_script_health", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _results(query_count: int, kept_count: int):
        class _R:
            def __init__(self, stats):
                self.retrieval_stats = stats

        return [_R({"web_query_count": query_count, "web_kept_count": kept_count})]

    def test_flags_phase_with_zero_web_evidence(self):
        script = self._script()

        health = script._retrieval_health(
            self._results(20, 0), self._results(28, 18)
        )

        assert health["degraded_phases"] == ["baseline"], "零产出的阶段必须被点名"
        assert health["baseline"]["degraded"] is True
        assert health["improved"]["degraded"] is False
        assert health["improved"]["yield_rate"] == round(18 / 28, 4)

    def test_no_degradation_when_both_phases_yield(self):
        script = self._script()

        health = script._retrieval_health(self._results(10, 4), self._results(10, 6))

        assert health["degraded_phases"] == []

    def test_yield_rate_is_none_when_no_queries_ran(self):
        """没检索过不等于检索失败 —— 不能报 degraded。"""
        script = self._script()

        health = script._retrieval_health(self._results(0, 0), self._results(0, 0))

        assert health["baseline"]["yield_rate"] is None
        assert health["degraded_phases"] == []
