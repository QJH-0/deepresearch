"""T1.4 证据评分 LLM 融合单元测试。

覆盖：
- T1.4-01 融合公式计算
- T1.4-02 LLM 输出带 markdown 包裹仍可解析
- T1.4-03 部分证据缺评回退先验
- T1.4-04 非法分数 clamp
- T1.4-05 LLM 整体异常回退先验
- T1.4-06 批量分批与上限
- T1.4-07 local 证据走融合仍高可信
- T1.4-08 融合分数驱动审计标记
- T1.4-09 prompt 结构校验
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from mult_agents.nodes._evidence import EvidenceScorer, _score_evidence


# ── 辅助 ─

def _mock_llm(response_content: str):
    llm = MagicMock()
    resp = MagicMock()
    resp.content = response_content
    llm.invoke.return_value = resp
    return llm


def _make_web_record(sid="WEB1_1-1", domain="example.com", title="t", snippet="s"):
    return {
        "source_id": sid,
        "domain": domain,
        "source_type": "web",
        "title": title,
        "snippet": snippet,
        "url": f"https://{domain}/page",
        "query": "test query",
    }


def _make_local_record(sid="LOC-1", doc_id="doc1", title="t", snippet="s"):
    return {
        "source_id": sid,
        "doc_id": doc_id,
        "source_type": "local",
        "title": title,
        "snippet": snippet,
        "query": "test query",
    }


# ── T1.4-01 融合公式计算 ──

def test_fusion_formula():
    """prior=0.58（普通域名），LLM score=0.8 → final=0.4*0.58+0.6*0.8。"""
    record = _make_web_record(domain="example.com")
    llm_json = json.dumps([{"source_id": "WEB1_1-1", "score": 0.8, "reason": "内容具体"}])
    llm = _mock_llm(llm_json)
    scorer = EvidenceScorer(llm, prior_weight=0.4)
    result = scorer.score_batch([record])
    assert len(result) == 1
    expected = 0.4 * 0.58 + 0.6 * 0.8
    assert result[0]["reliability_score"] == pytest.approx(expected, abs=1e-4)
    assert "内容具体" in result[0]["reliability_reason"]
    assert "先验0.58" in result[0]["reliability_reason"]


# ── T1.4-02 LLM 输出带 markdown 包裹仍可解析 ──

def test_markdown_wrapped_json():
    """LLM 返回 ```json\n[...]\n``` 包裹的合法 JSON。"""
    record = _make_web_record()
    wrapped = f"```json\n{json.dumps([{'source_id': 'WEB1_1-1', 'score': 0.8, 'reason': '内容具体'}])}\n```"
    llm = _mock_llm(wrapped)
    scorer = EvidenceScorer(llm, prior_weight=0.4)
    result = scorer.score_batch([record])
    expected = 0.4 * 0.58 + 0.6 * 0.8
    assert result[0]["reliability_score"] == pytest.approx(expected, abs=1e-4)


# ── T1.4-03 部分证据缺评回退先验 ──

def test_partial_missing_fallback_prior():
    """两批输入，LLM 只返回其中一条的评分。"""
    r1 = _make_web_record(sid="WEB1_1-1", domain="example.com")
    r2 = _make_web_record(sid="WEB1_1-2", domain="other.com")
    llm_json = json.dumps([{"source_id": "WEB1_1-1", "score": 0.8, "reason": "内容具体"}])
    llm = _mock_llm(llm_json)
    scorer = EvidenceScorer(llm, prior_weight=0.4)
    result = scorer.score_batch([r1, r2])
    assert len(result) == 2
    # r1 有 LLM 评分 → 融合分
    expected_r1 = 0.4 * 0.58 + 0.6 * 0.8
    assert result[0]["reliability_score"] == pytest.approx(expected_r1, abs=1e-4)
    # r2 无 LLM 评分 → 纯先验
    assert result[1]["reliability_score"] == 0.58
    assert "普通" in result[1]["reliability_reason"]


# ── T1.4-04 非法分数 clamp ──

def test_score_clamp():
    """LLM 返回 score=1.7 与 score=-0.5，应 clamp 到 [0,1]。"""
    r1 = _make_web_record(sid="WEB1_1-1")
    r2 = _make_web_record(sid="WEB1_1-2")
    llm_json = json.dumps([
        {"source_id": "WEB1_1-1", "score": 1.7, "reason": "高分"},
        {"source_id": "WEB1_1-2", "score": -0.5, "reason": "低分"},
    ])
    llm = _mock_llm(llm_json)
    scorer = EvidenceScorer(llm, prior_weight=0.4)
    result = scorer.score_batch([r1, r2])
    # 1.7 → 1.0, 融合 = 0.4*0.58+0.6*1.0
    assert result[0]["reliability_score"] == pytest.approx(0.4 * 0.58 + 0.6 * 1.0, abs=1e-4)
    # -0.5 → 0.0, 融合 = 0.4*0.58+0.6*0.0
    assert result[1]["reliability_score"] == pytest.approx(0.4 * 0.58, abs=1e-4)
    assert 0.0 <= result[0]["reliability_score"] <= 1.0
    assert 0.0 <= result[1]["reliability_score"] <= 1.0


# ── T1.4-05 LLM 整体异常回退先验 ──

def test_llm_exception_fallback_prior():
    """mock invoke 抛异常，两条均返回先验分数。"""
    r1 = _make_web_record(sid="WEB1_1-1", domain="example.com")
    r2 = _make_web_record(sid="WEB1_1-2", domain="other.com")
    llm = MagicMock()
    llm.invoke.side_effect = RuntimeError("LLM unavailable")
    scorer = EvidenceScorer(llm, prior_weight=0.4)
    result = scorer.score_batch([r1, r2])
    assert len(result) == 2
    for item in result:
        assert item["reliability_score"] == 0.58
        assert "普通" in item["reliability_reason"]


# ── T1.4-06 批量分批与上限 ──

def test_batch_splitting():
    """25 条记录 → invoke 被调用 2 次（20+5）。"""
    records = [_make_web_record(sid=f"WEB1_1-{i}") for i in range(1, 26)]
    llm_json = "[]"
    llm = _mock_llm(llm_json)
    scorer = EvidenceScorer(llm, prior_weight=0.4)
    result = scorer.score_batch(records)
    assert len(result) == 25
    assert llm.invoke.call_count == 2


# ── T1.4-07 local 证据走融合仍高可信 ──

def test_local_evidence_high_confidence():
    """local 记录 prior=0.92, LLM 评 0.7 → 融合分介于 0.7~0.92。"""
    record = _make_local_record()
    llm_json = json.dumps([{"source_id": "LOC-1", "score": 0.7, "reason": "相关"}])
    llm = _mock_llm(llm_json)
    scorer = EvidenceScorer(llm, prior_weight=0.4)
    result = scorer.score_batch([record])
    score = result[0]["reliability_score"]
    assert 0.7 <= score <= 0.92
    assert score >= 0.6


# ── T1.4-08 融合分数驱动审计标记 ──

def test_audit_flag_low_confidence():
    """mock LLM 给 web 证据评 0.2（prior 0.58 → 融合约 0.35）→ low_confidence。"""
    from mult_agents.nodes._fallbacks import _fallback_audit

    state = {
        "query": "test",
        "web_evidence": [_make_web_record(domain="example.com")],
        "local_evidence": [],
    }
    llm_json = json.dumps([{"source_id": "WEB1_1-1", "score": 0.2, "reason": "不相关"}])
    llm = _mock_llm(llm_json)
    scorer = EvidenceScorer(llm, prior_weight=0.4)

    with patch("mult_agents.nodes._fallbacks._get_evidence_scorer", return_value=scorer):
        result = _fallback_audit(state)
    flags = result["audit_flags"]
    assert any(f["type"] == "low_confidence" for f in flags)

    # 开关关闭时不触发 low_confidence（纯先验 0.58 → 仍 < 0.6 触发，换 high prior 域名验证）
    state2 = {
        "query": "test",
        "web_evidence": [_make_web_record(domain="example.gov.cn")],
        "local_evidence": [],
    }
    with patch("mult_agents.nodes._fallbacks._get_evidence_scorer", return_value=None):
        result2 = _fallback_audit(state2)
    # prior=0.88 ≥ 0.6 → 不触发 low_confidence
    flags2 = result2["audit_flags"]
    assert not any(f["type"] == "low_confidence" for f in flags2)


# ── T1.4-09 prompt 结构校验 ──

def test_prompt_structure():
    """捕获 mock invoke 的 prompt 文本，断言含 query、source_id、title 与 JSON 约束。"""
    record = _make_web_record()
    llm_json = json.dumps([{"source_id": "WEB1_1-1", "score": 0.8, "reason": "ok"}])
    llm = _mock_llm(llm_json)
    scorer = EvidenceScorer(llm, prior_weight=0.4)
    scorer.score_batch([record])
    prompt_arg = llm.invoke.call_args[0][0]
    assert "test query" in prompt_arg
    assert "WEB1_1-1" in prompt_arg
    assert "t" in prompt_arg  # title
    assert "JSON" in prompt_arg
