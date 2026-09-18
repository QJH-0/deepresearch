"""证据预算控制测试（`nodes/_context.py`）。

背景：证据在 State 中跨轮累积，节点若每轮把全量证据塞进 prompt，token 数随迭代
线性增长，模型在长上下文上的召回率随之下降（context rot）。本模块是统一的
「按预算取证据」入口。

阈值语义：**防病态膨胀**，不是正常路径裁剪。默认值刻意放宽，只在证据量异常时生效。

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    python -m pytest app/test/test_context_budget.py -v
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
sys.path.insert(0, str(_APP_PATH))

from mult_agents.nodes import _context  # noqa: E402


def _item(source_id: str, score: float, snippet: str = "内容") -> dict:
    return {"source_id": source_id, "relevance_score": score, "snippet": snippet}


class TestSelectEvidence:
    """按可靠度/相关度取 top-k，并受字符预算约束。"""

    def test_returns_all_when_within_budget(self):
        """未超阈值时原样返回，且保持原顺序。"""
        items = [_item("A", 0.1), _item("B", 0.9), _item("C", 0.5)]

        result = _context.select_evidence(items, limit=10, budget_chars=100000)

        assert result == items, "未超阈值时不得重排或丢弃"

    def test_keeps_highest_scores_and_preserves_original_order(self):
        """超 limit 时保留高分项，但返回顺序仍是原始相对顺序。

        顺序有语义：prompt 里证据的排列影响引用编号的可读性，不能按分数重排。
        """
        items = [_item("A", 0.1), _item("B", 0.9), _item("C", 0.5), _item("D", 0.8)]

        result = _context.select_evidence(items, limit=2, budget_chars=100000)

        assert [item["source_id"] for item in result] == ["B", "D"], (
            "应保留分数最高的两条，且维持原相对顺序"
        )

    def test_reliability_score_wins_over_relevance(self):
        """reliability_score 存在时优先作为排序依据（deep_dive 审计后的分数）。"""
        items = [
            {"source_id": "LOW_REL_HIGH_REL_AB", "reliability_score": 0.9, "relevance_score": 0.1},
            {"source_id": "HIGH_REL_LOW_REL_AB", "reliability_score": 0.2, "relevance_score": 0.9},
        ]

        result = _context.select_evidence(items, limit=1, budget_chars=100000)

        assert result[0]["source_id"] == "LOW_REL_HIGH_REL_AB"

    def test_char_budget_truncates(self):
        """字符预算生效：超出预算的项被淘汰。"""
        items = [
            _item("BIG", 0.9, snippet="x" * 2000),
            _item("SMALL", 0.1, snippet="y"),
        ]

        result = _context.select_evidence(items, limit=10, budget_chars=500)

        assert [item["source_id"] for item in result] == ["SMALL"], (
            "高分但超预算的大项应被淘汰，让位给能装下的小项"
        )

    def test_always_keeps_at_least_one_item(self):
        """单条就超预算时仍保留一条，避免返回空列表导致节点误判「无证据」。"""
        items = [_item("HUGE", 0.9, snippet="x" * 5000)]

        result = _context.select_evidence(items, limit=10, budget_chars=100)

        assert len(result) == 1, "预算再小也不能返回空，否则下游会误判为无证据"

    def test_filters_non_dict_items(self):
        """非 dict 项被过滤，避免 json.dumps 阶段抛错。"""
        items = [_item("A", 0.9), "垃圾数据", None, 42]

        result = _context.select_evidence(items, limit=10, budget_chars=100000)

        assert [item["source_id"] for item in result] == ["A"]

    def test_reads_thresholds_from_business_settings(self, monkeypatch):
        """未显式传参时阈值来自业务配置。"""
        monkeypatch.setattr(
            "backend.config.settings.get_business_settings",
            lambda: SimpleNamespace(context_evidence_limit=1, context_evidence_budget_chars=100000),
        )
        items = [_item("A", 0.1), _item("B", 0.9)]

        result = _context.select_evidence(items)

        assert [item["source_id"] for item in result] == ["B"], "应使用配置里的 limit=1"

    def test_falls_back_to_defaults_when_config_unavailable(self, monkeypatch):
        """配置不可用时回落模块默认值，而不是抛错或返回空。"""

        def boom():
            raise RuntimeError("配置不可用")

        monkeypatch.setattr("backend.config.settings.get_business_settings", boom)
        items = [_item("A", 0.9)]

        result = _context.select_evidence(items)

        assert [item["source_id"] for item in result] == ["A"]


class TestCompactEvidence:
    """截断长文本字段，但保留全部结构化字段。"""

    def test_truncates_long_snippet_but_keeps_all_keys(self):
        item = {
            "source_id": "A",
            "snippet": "x" * 2000,
            "reliability_reason": "y" * 2000,
            "notes": "z" * 2000,
            "url": "https://a.com",
            "reliability_score": 0.8,
        }

        result = _context.compact_evidence([item], snippet_max_chars=100)[0]

        assert len(result["snippet"]) == 101, "超长 snippet 应被截断（含省略号）"
        assert len(result["reliability_reason"]) == 101
        assert len(result["notes"]) == 101
        assert set(result) == set(item), "只截断不丢字段：下游依赖 source_id 等做引用溯源"
        assert result["url"] == "https://a.com"
        assert result["reliability_score"] == 0.8

    def test_keeps_short_text_untouched(self):
        item = {"source_id": "A", "snippet": "短内容"}

        result = _context.compact_evidence([item], snippet_max_chars=100)[0]

        assert result["snippet"] == "短内容"

    def test_does_not_mutate_input(self):
        item = {"source_id": "A", "snippet": "x" * 500}

        _context.compact_evidence([item], snippet_max_chars=10)

        assert len(item["snippet"]) == 500, "不得就地修改 State 里的证据对象"


class TestEvidenceForPrompt:
    """节点入口：取证据 + 裁剪 + 压缩，一步到位。"""

    def test_uses_evidence_pool_first(self):
        state = {
            "evidence_pool": [{"source_id": "POOL", "reliability_score": 0.9}],
            "web_evidence": [{"source_id": "WEB", "relevance_score": 0.9}],
            "local_evidence": [],
        }

        result = _context.evidence_for_prompt(state, limit=10, budget_chars=100000)

        assert [item["source_id"] for item in result] == ["POOL"]

    def test_falls_back_to_raw_evidence_when_pool_empty(self):
        """evidence_pool 为空（如 deep_dive 之前的轮次）时用原始证据，避免空手进 prompt。"""
        state = {
            "evidence_pool": [],
            "web_evidence": [{"source_id": "WEB", "relevance_score": 0.9}],
            "local_evidence": [{"source_id": "LOC", "relevance_score": 0.8}],
        }

        result = _context.evidence_for_prompt(state, limit=10, budget_chars=100000)

        assert [item["source_id"] for item in result] == ["WEB", "LOC"]

    def test_output_is_json_serialisable(self):
        state = {
            "evidence_pool": [{"source_id": "A", "snippet": "x" * 2000}],
            "web_evidence": [],
            "local_evidence": [],
        }

        result = _context.evidence_for_prompt(state, limit=10, budget_chars=100000)

        json.dumps(result, ensure_ascii=False)

    def test_raw_evidence_for_prompt_reads_named_source(self):
        """裁判节点用原始证据：它自己就是 evidence_pool 的生产者，不能读自己的输出。"""
        state = {
            "evidence_pool": [{"source_id": "POOL"}],
            "web_evidence": [{"source_id": "WEB", "relevance_score": 0.9}],
            "local_evidence": [],
        }

        result = _context.raw_evidence_for_prompt(state, "web_evidence", limit=10, budget_chars=100000)

        assert [item["source_id"] for item in result] == ["WEB"]


class TestResearchNotes:
    """研究笔记：从结构化结论汇编，供后续轮次与报告附录使用。"""

    def test_build_note_assembles_from_structured_inputs(self):
        findings = [
            {"claim_id": "c1", "claim": "结论A"},
            {"claim_id": "c2", "claim": "结论B"},
            {"claim_id": "c3"},  # 无 claim 正文，跳过
        ]
        audit_flags = [
            {"type": "low_confidence", "target": "WEB1_1-1"},
            {"type": "conflict", "target": "忽略我"},
        ]

        note = _context.build_research_note({"iteration": 1}, findings, ["缺口A"], audit_flags)

        assert note["iteration"] == 1
        assert note["confirmed"] == ["结论A", "结论B"]
        assert note["open_questions"] == ["缺口A"]
        assert note["low_confidence_sources"] == ["WEB1_1-1"], "只收 low_confidence 类型"

    def test_build_note_truncates_long_claims(self):
        long_claim = "结" * 500

        note = _context.build_research_note({}, [{"claim": long_claim}], [], [])

        assert len(note["confirmed"][0]) == _context.CLAIM_PREVIEW_CHARS, (
            "笔记要控制体积，长结论需截断（完整结论仍在 findings 里）"
        )

    def test_render_notes_covers_all_sections(self):
        notes = [{
            "iteration": 0,
            "confirmed": ["结论A"],
            "open_questions": ["缺口A"],
            "low_confidence_sources": ["WEB1_1-1"],
        }]

        text = _context.render_research_notes(notes)

        assert "[第 1 轮]" in text
        assert "已确认：结论A" in text
        assert "未解决：缺口A" in text
        assert "WEB1_1-1" in text

    def test_render_notes_skips_empty_sections(self):
        text = _context.render_research_notes([{"iteration": 0, "confirmed": ["A"]}])

        assert "已确认：A" in text
        assert "未解决" not in text
        assert "低可信来源" not in text

    def test_render_empty_notes_returns_empty_string(self):
        assert _context.render_research_notes([]) == ""
        assert _context.render_research_notes(None) == ""


class TestAnalyzeNodeWiring:
    """接线验证：analyze 真正用上预算与笔记，而不只是模块里存在这两个函数。"""

    @staticmethod
    def _draft(**overrides):
        from mult_agents.output_schemas import AnalysisDraft, Claim

        payload = {
            "analysis_summary": "小结",
            "needs_more_research": True,
            "missing_gaps": ["缺口A"],
            "findings": [Claim(claim_id="c1", claim="结论A", source_ids=["S1"])],
        }
        payload.update(overrides)
        return AnalysisDraft(**payload)

    async def test_returns_single_note_entry(self, monkeypatch):
        from mult_agents.nodes import analyze as analyze_module

        async def fake_invoke(state, prompt, agent, agent_name, node, writer=None):
            return self._draft(), []

        monkeypatch.setattr(analyze_module, "_invoke_structured_agent", fake_invoke)
        state = {
            "query": "q", "iteration": 1, "hitl_enabled": False,
            "sub_questions": [], "evidence_pool": [], "audit_flags": [],
        }

        out = await analyze_module.analyze_node(state, None, "analyst")

        assert len(out["research_notes"]) == 1, "只返回本轮一条，累积由 reducer 负责"
        assert out["research_notes"][0]["confirmed"] == ["结论A"]
        assert out["research_notes"][0]["open_questions"] == ["缺口A"]

    async def test_prompt_evidence_is_bounded(self, monkeypatch):
        """证据超阈值时，进入 prompt 的证据条数受控。"""
        from mult_agents.nodes import analyze as analyze_module

        monkeypatch.setattr(
            "backend.config.settings.get_business_settings",
            lambda: SimpleNamespace(context_evidence_limit=5, context_evidence_budget_chars=1000000),
        )
        captured = {}

        async def fake_invoke(state, prompt, agent, agent_name, node, writer=None):
            captured["prompt"] = prompt
            return self._draft(), []

        monkeypatch.setattr(analyze_module, "_invoke_structured_agent", fake_invoke)
        state = {
            "query": "q", "iteration": 0, "hitl_enabled": False,
            "sub_questions": [], "audit_flags": [],
            "evidence_pool": [
                {"source_id": f"S{index}", "snippet": "内容", "reliability_score": index / 100}
                for index in range(100)
            ],
        }

        await analyze_module.analyze_node(state, None, "analyst")

        prompt = captured["prompt"]
        evidence_marker = '"source_id"'
        assert prompt.count(evidence_marker) == 5, (
            f"prompt 里的证据条数应受 context_evidence_limit 约束，"
            f"实际 {prompt.count(evidence_marker)}"
        )
        assert '"S99"' in prompt, "应保留分数最高的一条"


class TestReportAppendixResearchNotes:
    """研究笔记进入报告附录，用户能看到研究过程的推进。"""

    def test_appendix_renders_research_progress(self):
        from mult_agents.nodes._fallbacks import _render_execution_appendix

        state = {
            "research_notes": [{
                "iteration": 0,
                "confirmed": ["结论A"],
                "open_questions": ["缺口A"],
                "low_confidence_sources": ["WEB1_1-1"],
            }],
            "search_plan": [],
            "research_questions": [],
            "web_retrieval_stats": {},
            "local_retrieval_stats": {},
            "iteration": 0,
        }

        text = _render_execution_appendix(state)

        assert "### 研究过程" in text
        assert "第 1 轮" in text
        assert "已确认: 结论A" in text
        assert "待解决: 缺口A" in text
        assert "低可信来源: WEB1_1-1" in text

    def test_appendix_handles_missing_notes(self):
        """无笔记（如未进入反思循环）时不得抛错。"""
        from mult_agents.nodes._fallbacks import _render_execution_appendix

        text = _render_execution_appendix({
            "search_plan": [], "research_questions": [],
            "web_retrieval_stats": {}, "local_retrieval_stats": {}, "iteration": 0,
        })

        assert "### 研究过程" in text
        assert "- 无" in text
