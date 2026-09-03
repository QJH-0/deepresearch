"""Phase 7/8 测试：引用溯源——来源去重编号、悬挂引用剔除、参考列表渲染。

覆盖用例:
    T7-1 来源去重与编号稳定（同 url 两轮检索 → sources 仅一条且编号不变；web+kb 混合去重键正确）
    T7-2 悬挂引用剔除（报告含 [WEB5_1-5] 但来源仅 3 条 → 后处理剔除该角标）

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    python -m pytest app/test/test_citations.py -v
"""

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
sys.path.insert(0, str(_APP_PATH))

from mult_agents.nodes._evidence import _dedupe_sources  # noqa: E402
from mult_agents.nodes._fallbacks import (  # noqa: E402
    _extract_citation_ids,
    _validate_and_fix_citations,
    _build_source_lookup,
    _render_reference_list,
    _ensure_reference_section,
)


# ──────────────────────────────────────────────
# T7-1 来源去重与编号稳定
# ──────────────────────────────────────────────


class TestSourceDedup:
    """来源去重与编号稳定性测试。"""

    def test_dedupe_by_url_web(self):
        """同 url 两轮检索 → sources 仅一条。"""
        records = [
            {"source_id": "WEB1_1-1", "url": "https://example.com/a", "title": "A", "source_type": "web"},
            {"source_id": "WEB1_1-2", "url": "https://example.com/a", "title": "A 重复", "source_type": "web"},
        ]
        result = _dedupe_sources(records, ["url"])
        assert len(result) == 1, "同 url 应去重为 1 条"
        assert result[0]["source_id"] == "WEB1_1-1", "应保留首次出现的记录"

    def test_dedupe_by_chunk_id_kb(self):
        """同 chunk_id 的 kb 来源去重。"""
        records = [
            {"source_id": "LOC1_1-1", "doc_id": "doc_001", "chunk_id": "chunk_5", "title": "KB-A", "source_type": "local"},
            {"source_id": "LOC1_1-3", "doc_id": "doc_001", "chunk_id": "chunk_5", "title": "KB-A 重复", "source_type": "local"},
        ]
        result = _dedupe_sources(records, ["chunk_id"])
        assert len(result) == 1
        assert result[0]["source_id"] == "LOC1_1-1"

    def test_dedupe_mixed_web_and_kb_different_keys(self):
        """web + kb 混合去重键正确（url 对 web, chunk_id 对 kb）。"""
        records = [
            {"source_id": "WEB1_1-1", "url": "https://a.com", "chunk_id": "", "title": "Web1", "source_type": "web"},
            {"source_id": "WEB1_1-2", "url": "https://a.com", "chunk_id": "", "title": "Web1 dup", "source_type": "web"},
            {"source_id": "LOC1_1-1", "url": "", "chunk_id": "c3", "title": "KB1", "source_type": "local"},
            {"source_id": "LOC1_1-2", "url": "", "chunk_id": "c3", "title": "KB1 dup", "source_type": "local"},
        ]
        # 用 source_id 做去重
        result = _dedupe_sources(records, ["source_id"])
        assert len(result) == 4, "source_id 各不相同，全部保留"
        # 用 url 做去重（web 有 url，kb 无 url → 空字符串作为 key）
        result_url = _dedupe_sources(records, ["url"])
        # web 去重 1 条 + kb url 为空但去重后空字符串合并 → 验证 web 去重
        web_kept = [r for r in result_url if r.get("source_type") == "web"]
        assert len(web_kept) == 1, "web 同 url 去重后应剩 1 条"

    def test_dedupe_preserves_order(self):
        """去重后保留首次出现顺序（编号稳定性）。"""
        records = [
            {"source_id": "WEB1_1-3", "url": "https://c.com", "title": "C"},
            {"source_id": "WEB1_1-1", "url": "https://a.com", "title": "A"},
            {"source_id": "WEB1_1-2", "url": "https://b.com", "title": "B"},
            {"source_id": "WEB1_1-1", "url": "https://a.com", "title": "A dup"},
        ]
        result = _dedupe_sources(records, ["source_id"])
        assert len(result) == 3
        assert result[0]["source_id"] == "WEB1_1-3"
        assert result[1]["source_id"] == "WEB1_1-1"
        assert result[2]["source_id"] == "WEB1_1-2"

    def test_dedupe_empty_list(self):
        """空列表去重返回空列表。"""
        assert _dedupe_sources([], ["url"]) == []

    def test_dedupe_single_item(self):
        """单条不去重。"""
        records = [{"source_id": "WEB1_1-1", "url": "https://a.com"}]
        result = _dedupe_sources(records, ["url"])
        assert len(result) == 1


# ──────────────────────────────────────────────
# T7-2 悬挂引用剔除
# ──────────────────────────────────────────────


class TestCitationValidation:
    """引用校验与悬挂引用剔除测试。"""

    def test_extract_citation_ids_basic(self):
        """从正文中提取引用 ID。"""
        content = "这是论断 [WEB1_1-1]，还有 [LOC1_1-2] 的支撑。"
        ids = _extract_citation_ids(content)
        assert "WEB1_1-1" in ids
        assert "LOC1_1-2" in ids

    def test_extract_citation_ids_dedup(self):
        """同一引用 ID 出现多次只保留一次。"""
        content = "论断 [WEB1_1-1] 然后 [WEB1_1-1] 再次引用。"
        ids = _extract_citation_ids(content)
        assert len(ids) == 1
        assert ids[0] == "WEB1_1-1"

    def test_extract_citation_ids_empty(self):
        """无引用时返回空列表。"""
        ids = _extract_citation_ids("没有任何引用的文本")
        assert ids == []

    def test_validate_and_fix_citations_removes_dangling(self):
        """报告含 [WEB1_1-5] 但来源仅 3 条 → 剔除该角标。"""
        valid_ids = {"WEB1_1-1", "WEB1_1-2", "WEB1_1-3"}
        content = "论断A [WEB1_1-1]。论断B [WEB1_1-5]。论断C [WEB1_1-2]。"
        fixed, used = _validate_and_fix_citations(content, valid_ids)
        assert "WEB1_1-5" not in fixed, "悬挂引用应被剔除"
        assert "WEB1_1-1" in fixed, "合法引用保留"
        assert "WEB1_1-2" in fixed, "合法引用保留"
        assert "WEB1_1-5" not in used
        assert "WEB1_1-1" in used
        assert "WEB1_1-2" in used

    def test_validate_and_fix_citations_all_valid(self):
        """全部合法引用保留。"""
        valid_ids = {"WEB1_1-1", "WEB1_1-2"}
        content = "论断 [WEB1_1-1] 和 [WEB1_1-2]。"
        fixed, used = _validate_and_fix_citations(content, valid_ids)
        assert fixed == content
        assert len(used) == 2

    def test_validate_and_fix_citations_all_dangling(self):
        """全部为悬挂引用 → 全部剔除。"""
        valid_ids = {"WEB1_1-1"}
        content = "论断 [WEB1_1-5] 和 [LOC1_1-9]。"
        fixed, used = _validate_and_fix_citations(content, valid_ids)
        assert "WEB1_1-5" not in fixed
        assert "LOC1_1-9" not in fixed
        assert used == []

    def test_validate_and_fix_citations_multiple_same(self):
        """同一合法引用出现多次 → 全部保留。"""
        valid_ids = {"WEB1_1-1"}
        content = "[WEB1_1-1] 论断 [WEB1_1-1] 再 [WEB1_1-1]。"
        fixed, used = _validate_and_fix_citations(content, valid_ids)
        # used 去重，只有 1 个
        assert len(used) == 1
        assert "WEB1_1-1" in fixed


# ──────────────────────────────────────────────
# 参考列表渲染测试
# ──────────────────────────────────────────────


class TestReferenceList:
    """参考列表渲染测试。"""

    def test_build_source_lookup_from_state(self):
        """从 state 构建 source lookup。"""
        state = {
            "source_index": [
                {"source_id": "WEB1_1-1", "source_type": "web", "label": "来源A", "locator": "https://a.com"},
            ],
            "evidence_pool": [],
            "web_evidence": [
                {"source_id": "WEB1_1-2", "title": "来源B", "url": "https://b.com"},
            ],
            "local_evidence": [
                {"source_id": "LOC1_1-1", "title": "KB文档", "doc_id": "doc_1"},
            ],
        }
        lookup = _build_source_lookup(state)
        assert "WEB1_1-1" in lookup
        assert "WEB1_1-2" in lookup
        assert "LOC1_1-1" in lookup
        assert lookup["WEB1_1-1"]["locator"] == "https://a.com"
        assert lookup["LOC1_1-1"]["source_type"] == "local"

    def test_render_reference_list_empty_state(self):
        """空 state → 参考列表为「暂无参考资料」。"""
        state = {
            "source_index": [],
            "evidence_pool": [],
            "web_evidence": [],
            "local_evidence": [],
            "draft": "",
            "findings": [],
        }
        result = _render_reference_list(state)
        assert "暂无参考资料" in result

    def test_render_reference_list_with_citations(self):
        """有正文引用 → 参考列表按引用顺序排列。"""
        state = {
            "source_index": [
                {"source_id": "WEB1_1-1", "source_type": "web", "label": "来源A", "locator": "https://a.com"},
                {"source_id": "WEB1_1-2", "source_type": "web", "label": "来源B", "locator": "https://b.com"},
            ],
            "evidence_pool": [],
            "web_evidence": [],
            "local_evidence": [],
            "draft": "正文 [WEB1_1-2] 和 [WEB1_1-1]。",
            "findings": [],
        }
        result = _render_reference_list(state)
        assert "## 参考资料" in result
        assert "WEB1_1-2" in result
        assert "WEB1_1-1" in result

    def test_ensure_reference_section_appends(self):
        """无参考列表 → 追加。"""
        content = "这是报告正文。"
        state = {
            "source_index": [
                {"source_id": "WEB1_1-1", "source_type": "web", "label": "来源A", "locator": "https://a.com"},
            ],
            "evidence_pool": [],
            "web_evidence": [],
            "local_evidence": [],
            "draft": content,
            "findings": [],
        }
        result = _ensure_reference_section(content, state)
        assert "## 参考资料" in result
        assert content in result

    def test_ensure_reference_section_no_duplicate(self):
        """已有参考列表 → 不重复追加。"""
        content = "这是报告正文。\n\n## 参考资料\n- [WEB1_1-1] 来源A"
        state = {"source_index": [], "evidence_pool": [], "web_evidence": [], "local_evidence": [], "draft": content, "findings": []}
        result = _ensure_reference_section(content, state)
        # 不应该有两次 "## 参考资料"
        assert result.count("## 参考资料") == 1

    def test_local_source_dedup_by_locator(self):
        """local 来源按 locator 去重展示（同一文件多个 chunk 只展示一次）。"""
        state = {
            "source_index": [
                {"source_id": "LOC1_1-1", "source_type": "local", "label": "KB文档", "locator": "doc_1"},
                {"source_id": "LOC1_1-2", "source_type": "local", "label": "KB文档2", "locator": "doc_1"},
            ],
            "evidence_pool": [],
            "web_evidence": [],
            "local_evidence": [],
            "draft": "正文 [LOC1_1-1] 和 [LOC1_1-2]。",
            "findings": [],
        }
        result = _render_reference_list(state)
        # 只应出现一次 doc_1
        assert result.count("doc_1") == 1


# ──────────────────────────────────────────────
# 事件协议合规（来源相关）
# ──────────────────────────────────────────────


class TestSourcesFoundEvent:
    """sources.found 事件合规测试。"""

    def test_sources_found_event_constructible(self):
        """SourceItem 可从搜索结果 dict 构造。"""
        from backend.schemas.events import SourceItem, SourcesFoundData, event

        s1 = SourceItem(url="https://a.com", title="A", snippet="...", source_type="web")
        s2 = SourceItem(url="https://b.com", title="B", snippet="...", source_type="web")

        env = event("sources.found", sources=[s1.model_dump(), s2.model_dump()])
        assert env.type == "sources.found"
        assert len(env.data["sources"]) == 2
        assert env.data["sources"][0]["url"] == "https://a.com"

    def test_sources_found_with_kb_source(self):
        """SourceItem 支持 kb 类型（chunk_id）。"""
        from backend.schemas.events import SourceItem, event

        s = SourceItem(url=None, title="KB文档", snippet="片段", source_type="kb", chunk_id="chunk_42")
        env = event("sources.found", sources=[s.model_dump()])
        assert env.data["sources"][0]["source_type"] == "kb"
        assert env.data["sources"][0]["chunk_id"] == "chunk_42"

    def test_source_item_invalid_type_raises(self):
        """非法 source_type 报 ValidationError。"""
        from backend.schemas.events import SourceItem
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SourceItem(source_type="invalid")
