"""T1.3 qwen3-rerank 专用重排单元测试。

覆盖：
- T1.3-01 正常重排映射
- T1.3-02 候选截断保护
- T1.3-03 API 异常触发降级链
- T1.3-04 SDK 网络异常转 RerankUnavailable
- T1.3-05 API 返回非 200
- T1.3-06 结果为空不降级
- T1.3-07 配置开关
"""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from langchain_core.documents import Document

from mult_agents.rag.core import (
    DashScopeReranker,
    LLMReranker,
    RAGConfig,
    RAGSystem,
    RerankUnavailable,
)


# ── 辅助 ──

def _make_docs(n: int, prefix: str = "doc") -> list:
    return [Document(page_content=f"{prefix}-{i}", metadata={"idx": i}) for i in range(n)]


class _MockResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text or json.dumps(self._json)

    def json(self):
        return self._json


# ── T1.3-01 正常重排映射 ──

def test_normal_rerank_mapping():
    """mock dashscope rerank API 返回 results，验证映射回原 Document 列表。"""
    docs = _make_docs(4)
    mock_resp = _MockResponse(
        status_code=200,
        json_data={
            "output": {
                "results": [
                    {"index": 2, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.7},
                ]
            }
        },
    )
    with patch("mult_agents.rag.core.httpx.post", return_value=mock_resp):
        result = DashScopeReranker("fake-key").rerank("query", docs, top_k=3)
    assert len(result) <= 3
    assert result[0] is docs[2]
    assert result[1] is docs[0]


# ── T1.3-02 候选截断保护 ──

def test_candidate_truncation():
    """30 个 Document 验证 MAX_DOCS=20 截断；超长文档验证 MAX_DOC_CHARS=2000 截断。"""
    docs = _make_docs(30)
    long_doc = Document(page_content="x" * 5000)
    docs_with_long = [long_doc] + _make_docs(29)

    captured_body = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured_body["json"] = json
        return _MockResponse(
            status_code=200,
            json_data={"output": {"results": [{"index": 0, "relevance_score": 1.0}]}},
        )

    with patch("mult_agents.rag.core.httpx.post", side_effect=fake_post):
        DashScopeReranker("k").rerank("q", docs, top_k=5)

    sent_docs = captured_body["json"]["input"]["documents"]
    assert len(sent_docs) == 20  # MAX_DOCS

    with patch("mult_agents.rag.core.httpx.post", side_effect=fake_post):
        DashScopeReranker("k").rerank("q", docs_with_long, top_k=5)

    sent_doc_text = captured_body["json"]["input"]["documents"][0]
    assert len(sent_doc_text) == 2000  # MAX_DOC_CHARS


# ── T1.3-03 API 异常触发降级链 ──

def test_rerank_unavailable_triggers_llm_fallback():
    """RAGSystem._rerank 在 DashScopeReranker 抛 RerankUnavailable 时降级 LLMReranker。"""
    config = RAGConfig(enable_rerank_model=True, enable_reranker=True)
    rag = RAGSystem.__new__(RAGSystem)
    rag.config = config
    rag.api_key = "fake-key"
    rag._reranker_model = MagicMock(spec=DashScopeReranker)
    rag._reranker_model.rerank.side_effect = RerankUnavailable("timeout")
    rag._reranker = MagicMock(spec=LLMReranker)
    d1, d2 = _make_docs(2)
    rag._reranker.rerank.return_value = [d2, d1]

    result = rag._rerank("query", [d1, d2], top_k=2)
    assert result == [d2, d1]
    rag._reranker_model.rerank.assert_called_once()
    rag._reranker.rerank.assert_called_once()


# ── T1.3-04 SDK 网络异常转 RerankUnavailable ──

def test_network_exception_to_rerank_unavailable():
    """httpx.post 抛异常时 DashScopeReranker.rerank 应抛 RerankUnavailable。"""
    docs = _make_docs(3)
    with patch("mult_agents.rag.core.httpx.post", side_effect=Exception("timeout")):
        with pytest.raises(RerankUnavailable):
            DashScopeReranker("k").rerank("q", docs)


# ── T1.3-05 API 返回非 200 ──

def test_non_200_raises_rerank_unavailable():
    """status_code=429 时应抛 RerankUnavailable。"""
    docs = _make_docs(3)
    mock_resp = _MockResponse(status_code=429, text="rate limited")
    with patch("mult_agents.rag.core.httpx.post", return_value=mock_resp):
        with pytest.raises(RerankUnavailable):
            DashScopeReranker("k").rerank("q", docs)


# ── T1.3-06 结果为空不降级 ──

def test_empty_results_no_fallback():
    """API 返回 200 但 results=[] 时返回空列表，不触发降级。"""
    docs = _make_docs(3)
    mock_resp = _MockResponse(
        status_code=200,
        json_data={"output": {"results": []}},
    )
    with patch("mult_agents.rag.core.httpx.post", return_value=mock_resp):
        result = DashScopeReranker("k").rerank("q", docs)
    assert result == []


# ── T1.3-07 配置开关 ──

def test_config_disable_rerank_model():
    """enable_rerank_model=False 时 DashScopeReranker 不被实例化。"""
    config = RAGConfig(enable_rerank_model=False, enable_reranker=True)
    rag = RAGSystem.__new__(RAGSystem)
    rag.config = config
    rag.api_key = "fake-key"
    rag._reranker_model = None
    rag._reranker = MagicMock(spec=LLMReranker)
    d1, d2 = _make_docs(2)
    rag._reranker.rerank.return_value = [d2, d1]

    result = rag._rerank("query", [d1, d2], top_k=2)
    assert result == [d2, d1]
    rag._reranker.rerank.assert_called_once()
