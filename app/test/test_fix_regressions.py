"""2026-09-14 修复批次回归测试。

覆盖审查报告第二篇确认的「宣称可用但实际断链」的四条链路，以及若干工程卫生修复。
每个用例对应一个已修复缺陷，断言写成「失败即代表缺陷复现」的形式。
"""

import pathlib
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException


# ──────────────────────────────────────────────────────────────
# P1-1 长期记忆：写入命名空间模板必须与读取前缀解析一致
# ──────────────────────────────────────────────────────────────


class TestMemoryNamespaceConsistency:
    def test_write_template_resolves_to_read_prefix(self):
        from backend.service.memory_service import (
            MEMORY_NAMESPACE_SUFFIX,
            MEMORY_NAMESPACE_USER,
            _user_namespace,
        )

        write_ns = tuple(
            part.format(user_id="u1")
            for part in (MEMORY_NAMESPACE_USER, MEMORY_NAMESPACE_SUFFIX)
        )
        assert write_ns == _user_namespace("u1")
        assert write_ns == ("u1", "memories")

    async def test_hot_path_search_queries_the_same_namespace(self, monkeypatch):
        from backend.service import memory_service

        captured = {}

        class FakeStore:
            async def asearch(self, namespace, query=None, limit=None):
                captured["namespace"] = namespace
                return []

        monkeypatch.setattr(memory_service, "get_store", lambda: FakeStore())
        svc = memory_service.MemoryService(api_key="test-key")
        await svc.hot_path_search("u1", "q")

        assert captured["namespace"] == memory_service._user_namespace("u1")

    async def test_put_and_list_use_the_same_namespace(self, monkeypatch):
        from backend.service import memory_service

        seen = []

        class FakeStore:
            async def aput(self, namespace, key, value, index=None):
                seen.append(("aput", namespace))

            async def asearch(self, namespace, query=None, limit=None):
                seen.append(("asearch", namespace))
                return []

        monkeypatch.setattr(memory_service, "get_store", lambda: FakeStore())
        svc = memory_service.MemoryService(api_key="test-key")
        await svc.put_memory("u1", "text")
        await svc.list_memories("u1")

        assert seen[0][1] == seen[1][1] == memory_service._user_namespace("u1")


# ──────────────────────────────────────────────────────────────
# P1-6 相关性过滤接线 + relevance_score 透传
# ──────────────────────────────────────────────────────────────


def _web_search_state(query="LangGraph 编排"):
    return {
        "query": query,
        "search_plan": [
            {"query": query, "source_preference": "web", "section_id": "sec_1", "reason": "t"}
        ],
        "web_search_trace": [],
        "web_retrieval_stats": {},
        "sub_questions": [],
    }


class TestRelevanceFilterWiring:
    def test_enrich_evidence_carries_relevance_score(self):
        from mult_agents.nodes._evidence import _enrich_evidence_from_raw

        raw = [{"source_id": "WEB1_1-1", "relevance_score": 0.75, "url": "u", "title": "t"}]
        out = _enrich_evidence_from_raw([{"source_id": "WEB1_1-1", "snippet": "s"}], raw)

        assert out[0]["relevance_score"] == 0.75

    async def test_web_search_node_drops_irrelevant_and_scores_kept(self, monkeypatch):
        """接线回归：无关记录必须被剔除，保留记录必须带 relevance_score。"""
        from mult_agents.nodes import web_search

        records = [
            {"title": "LangGraph 教程", "url": "https://a.com/1",
             "snippet": "LangGraph 是编排框架", "domain": "a.com"},
            {"title": "无关内容", "url": "https://b.com/2",
             "snippet": "今天天气不错", "domain": "b.com"},
        ]

        def fake_search(query, count=4):
            return [dict(r) for r in records]

        async def fake_invoke(state, prompt, agent, agent_name, node, fallback, writer=None):
            return fallback, "", []

        monkeypatch.setattr(web_search, "web_search_records", fake_search)
        monkeypatch.setattr(web_search, "_invoke_json_agent", fake_invoke)

        out = await web_search.web_search_node(_web_search_state(), None, "scout_web")

        evidence = out["web_evidence"]
        assert len(evidence) == 1, f"无关记录未被剔除: {evidence}"
        assert evidence[0]["title"] == "LangGraph 教程"
        assert evidence[0]["relevance_score"] >= 0.2

    def test_sufficiency_is_not_falsely_negative_for_relevant_evidence(self):
        """回归：max_relevance 恒为 0 导致「证据显著不足」误判。"""
        from mult_agents.nodes._fallbacks import _check_evidence_sufficiency

        state = {
            "query": "q",
            "web_evidence": [{"source_id": "W1", "relevance_score": 0.9}],
            "local_evidence": [{"source_id": "L1", "relevance_score": 0.8}],
            "web_retrieval_stats": {},
            "local_retrieval_stats": {},
        }

        ok, reason = _check_evidence_sufficiency(state)

        assert ok is True, f"相关证据被误判为不足: {reason}"


# ──────────────────────────────────────────────────────────────
# P1-7 EvidenceScorer 可达性
# ──────────────────────────────────────────────────────────────


class TestEvidenceScorerReachable:
    def test_scorer_available_when_fusion_enabled(self, monkeypatch):
        from mult_agents.nodes import _fallbacks

        monkeypatch.setattr(_fallbacks, "_get_scorer_llm", lambda: MagicMock())
        monkeypatch.setattr(
            "backend.config.settings.get_business_settings",
            lambda: SimpleNamespace(evidence_llm_fusion=True, evidence_prior_weight=0.35),
        )

        scorer = _fallbacks._get_evidence_scorer()

        assert scorer is not None, "evidence_llm_fusion 开启时 scorer 不应为 None"
        assert scorer._prior_weight == 0.35

    def test_scorer_none_when_fusion_disabled(self, monkeypatch):
        from mult_agents.nodes import _fallbacks

        monkeypatch.setattr(_fallbacks, "_get_scorer_llm", lambda: MagicMock())
        monkeypatch.setattr(
            "backend.config.settings.get_business_settings",
            lambda: SimpleNamespace(evidence_llm_fusion=False, evidence_prior_weight=0.4),
        )

        assert _fallbacks._get_evidence_scorer() is None

    def test_scorer_none_when_llm_unavailable(self, monkeypatch):
        from mult_agents.nodes import _fallbacks

        monkeypatch.setattr(_fallbacks, "_get_scorer_llm", lambda: None)
        monkeypatch.setattr(
            "backend.config.settings.get_business_settings",
            lambda: SimpleNamespace(evidence_llm_fusion=True, evidence_prior_weight=0.4),
        )

        assert _fallbacks._get_evidence_scorer() is None

    def test_fallback_audit_passes_research_query_to_scorer(self, monkeypatch):
        """回归：评分 prompt 里的「研究问题」曾恒为空。"""
        from mult_agents.nodes import _fallbacks

        spy = MagicMock()
        spy.score_batch.return_value = []
        monkeypatch.setattr(_fallbacks, "_get_evidence_scorer", lambda: spy)

        _fallbacks._fallback_audit({
            "query": "研究问题 X",
            "web_evidence": [{"source_id": "W1"}],
            "local_evidence": [],
        })

        assert spy.score_batch.call_args.kwargs.get("query") == "研究问题 X"


# ──────────────────────────────────────────────────────────────
# P1-8 content_tsv 生成列 + RAG 配置统一
# ──────────────────────────────────────────────────────────────


class TestFulltextColumnAndRagConfig:
    def test_ddl_declares_generated_column_and_gin_index(self):
        from backend.infra.postgres_client import DDL_DOCUMENT_CHUNKS_FULLTEXT

        assert "content_tsv" in DDL_DOCUMENT_CHUNKS_FULLTEXT
        assert "GENERATED ALWAYS AS" in DDL_DOCUMENT_CHUNKS_FULLTEXT
        assert "GIN" in DDL_DOCUMENT_CHUNKS_FULLTEXT
        assert "IF NOT EXISTS" in DDL_DOCUMENT_CHUNKS_FULLTEXT

    def test_ensure_tables_executes_fulltext_ddl(self, monkeypatch):
        from backend.infra import postgres_client as pc

        executed = []

        class FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql):
                executed.append(sql)

        class FakeConn:
            autocommit = False

            def cursor(self):
                return FakeCursor()

            def close(self):
                pass

        monkeypatch.setattr(pc.psycopg, "connect", lambda dsn: FakeConn())
        pc.ensure_tables("postgresql://example/db")

        assert any("content_tsv" in sql for sql in executed), "ensure_tables 未执行全文检索 DDL"

    def test_rag_config_defaults_come_from_shared_constants(self):
        from mult_agents.rag.core import (
            DEFAULT_CHILD_COLLECTION,
            DEFAULT_PARENT_COLLECTION,
            RAGConfig,
        )

        cfg = RAGConfig()
        assert cfg.collection_name == DEFAULT_CHILD_COLLECTION
        assert cfg.parent_collection_name == DEFAULT_PARENT_COLLECTION

    def test_call_sites_do_not_hardcode_collection_names(self):
        """三个调用点都不得各自指定 collection，否则写入与检索会落到不同集合。"""
        app_root = pathlib.Path(__file__).resolve().parents[1]
        targets = [
            app_root / "app_main.py",
            app_root / "mult_agents" / "runtime.py",
            app_root / "backend" / "service" / "document_service.py",
        ]
        for target in targets:
            src = target.read_text(encoding="utf-8")
            assert "collection_name=" not in src, (
                f"{target.name} 硬编码了 collection_name，会与 RAGConfig 默认常量分叉"
            )


# ──────────────────────────────────────────────────────────────
# P1-9 RRF：每个查询变体必须是独立的一路召回
# ──────────────────────────────────────────────────────────────


class TestRrfMultiRunFusion:
    def test_each_query_variant_is_a_separate_run(self, monkeypatch):
        from mult_agents.rag import core
        from mult_agents.rag.core import Document, RAGSystem

        monkeypatch.setattr(core.utility, "has_collection", lambda name: True)

        captured = {}
        real_rrf = core.rrf_fuse

        def spy(result_lists, k=60, top_k=None, doc_key=None):
            captured["n_lists"] = len(result_lists)
            return real_rrf(result_lists, k=k, top_k=top_k, doc_key=doc_key)

        monkeypatch.setattr(core, "rrf_fuse", spy)

        variants = ["q1", "q2", "q3"]
        rag = MagicMock()
        rag.config.enable_query_rewrite = True
        rag.config.enable_bm25 = True
        rag.config.enable_reranker = False
        rag.config.enable_parent_child = False
        rag.config.recall_k = 5
        rag.config.rrf_k = 60
        # MagicMock 不走真实类的 property，需直接挂在 query_rewriter 上
        rag.query_rewriter.rewrite.return_value = variants
        rag.vectorstore.similarity_search.return_value = [
            Document(page_content="d", metadata={"parent_id": "", "source": "s"})
        ]
        rag._keyword_retriever = None
        rag.bm25._documents = []

        RAGSystem.search_records(rag, "query", k=2)

        assert captured["n_lists"] == len(variants) + 1, (
            "向量多路召回被拍平成一个列表，RRF 语义被破坏"
        )
        assert rag.vectorstore.similarity_search.call_count == len(variants)


# ──────────────────────────────────────────────────────────────
# P1-10 同步阻塞调用必须移出事件循环
# ──────────────────────────────────────────────────────────────


class TestSyncWorkOffEventLoop:
    async def test_web_search_node_runs_sync_search_off_event_loop(self, monkeypatch):
        from mult_agents.nodes import web_search

        captured = {}

        def fake_sync_search(query, count=4):
            captured["thread"] = threading.current_thread()
            return []

        monkeypatch.setattr(web_search, "web_search_records", fake_sync_search)

        await web_search.web_search_node(_web_search_state(), None, "scout_web")

        assert captured["thread"] is not threading.main_thread(), (
            "同步检索仍在事件循环线程上执行，会阻塞其他 SSE 流"
        )

    async def test_local_rag_node_runs_sync_retrieval_off_event_loop(self, monkeypatch):
        from mult_agents.nodes import local_rag

        captured = {}

        def fake_sync_search(query, limit=5):
            captured["thread"] = threading.current_thread()
            return []

        monkeypatch.setattr(local_rag, "search_knowledge_base_records", fake_sync_search)

        state = {
            "query": "LangGraph",
            "search_plan": [
                {"query": "LangGraph", "source_preference": "local", "section_id": "sec_1"}
            ],
            "local_rag_trace": [],
            "local_retrieval_stats": {},
            "sub_questions": [],
        }
        await local_rag.local_rag_node(state, None, "scout_local")

        assert captured["thread"] is not threading.main_thread(), (
            "同步本地检索仍在事件循环线程上执行，会阻塞其他 SSE 流"
        )


# ──────────────────────────────────────────────────────────────
# P2-11 官方域名判定收紧
# ──────────────────────────────────────────────────────────────


class TestOfficialDomainJudgement:
    @pytest.mark.parametrize(
        "domain,expected",
        [
            ("foo.gov.cn", True),
            ("bar.edu", True),
            ("x.ac.cn", True),
            ("army.mil", True),
            ("govtech.com", False),
            ("mygovnews.cn", False),
            ("example.com", False),
            ("", False),
        ],
    )
    def test_suffix_based_only(self, domain, expected):
        from mult_agents.nodes._evidence import _is_official_domain

        assert _is_official_domain(domain) is expected


# ──────────────────────────────────────────────────────────────
# P2-7 上传大小限制：分块读取、超限即断
# ──────────────────────────────────────────────────────────────


class _FakeUpload:
    """模拟 UploadFile：按分块产出，记录 read 调用次数。"""

    def __init__(self, total_bytes, chunk_bytes=64):
        self._left = total_bytes
        self._chunk = chunk_bytes
        self.read_calls = 0

    async def read(self, size=-1):
        self.read_calls += 1
        if self._left <= 0:
            return b""
        n = min(size if size > 0 else self._left, self._left, self._chunk)
        self._left -= n
        return b"x" * n


class TestUploadSizeGuard:
    async def test_oversized_upload_rejected_early(self, monkeypatch):
        from importlib import import_module

        dr = import_module("backend.router.document_router")

        monkeypatch.setattr(dr, "MAX_FILE_SIZE_BYTES", 8)
        upload = _FakeUpload(total_bytes=10_000)

        with pytest.raises(HTTPException) as exc:
            await dr._read_within_limit(upload)

        assert exc.value.status_code == 413
        # 证明是「边读边判」而非先全量读入：只读了 1 次就中断
        assert upload.read_calls == 1

    async def test_empty_upload_rejected(self):
        from importlib import import_module

        dr = import_module("backend.router.document_router")

        with pytest.raises(HTTPException) as exc:
            await dr._read_within_limit(_FakeUpload(total_bytes=0))

        assert exc.value.status_code == 400

    async def test_normal_upload_passes_through(self):
        from importlib import import_module

        dr = import_module("backend.router.document_router")

        content = await dr._read_within_limit(_FakeUpload(total_bytes=100))

        assert content == b"x" * 100


# ──────────────────────────────────────────────────────────────
# P0-2（局部）admin 端点：非 development 环境 fail-closed
# ──────────────────────────────────────────────────────────────


class TestAdminTokenFailClosed:
    def test_production_without_token_denied(self, monkeypatch):
        from importlib import import_module

        ar = import_module("backend.router.admin_router")

        monkeypatch.setattr(ar, "MiddlewareSettings", lambda: SimpleNamespace(admin_token=""))
        monkeypatch.setattr(ar, "AppSettings", lambda: SimpleNamespace(app_env="production"))

        with pytest.raises(HTTPException) as exc:
            ar._check_admin_token(None)

        assert exc.value.status_code == 401

    def test_development_without_token_allowed(self, monkeypatch):
        from importlib import import_module

        ar = import_module("backend.router.admin_router")

        monkeypatch.setattr(ar, "MiddlewareSettings", lambda: SimpleNamespace(admin_token=""))
        monkeypatch.setattr(ar, "AppSettings", lambda: SimpleNamespace(app_env="development"))

        ar._check_admin_token(None)  # 不抛异常

    def test_token_mismatch_denied(self, monkeypatch):
        from importlib import import_module

        ar = import_module("backend.router.admin_router")

        monkeypatch.setattr(ar, "MiddlewareSettings", lambda: SimpleNamespace(admin_token="secret"))
        monkeypatch.setattr(ar, "AppSettings", lambda: SimpleNamespace(app_env="production"))

        with pytest.raises(HTTPException) as exc:
            ar._check_admin_token("wrong")

        assert exc.value.status_code == 401

    def test_token_match_allowed(self, monkeypatch):
        from importlib import import_module

        ar = import_module("backend.router.admin_router")

        monkeypatch.setattr(ar, "MiddlewareSettings", lambda: SimpleNamespace(admin_token="secret"))
        monkeypatch.setattr(ar, "AppSettings", lambda: SimpleNamespace(app_env="production"))

        ar._check_admin_token("secret")  # 不抛异常


# ──────────────────────────────────────────────────────────────
# P2-3 interrupt kind 兜底值可序列化
# ──────────────────────────────────────────────────────────────


class TestInterruptKindFallback:
    def test_unknown_kind_is_a_valid_event(self):
        from backend.schemas.events import event

        envelope = event("interrupt.raised", interrupt_id="i1", kind="unknown", payload={})

        assert envelope.data["kind"] == "unknown"


# ──────────────────────────────────────────────────────────────
# P1-5 HITL 协议拆分：clarification 与 evidence_gap 各自独立
# ──────────────────────────────────────────────────────────────


class TestInterruptProtocolSplit:
    def test_clarification_validated_by_own_schema(self):
        from backend.router.research_router import _validate_resume_payload

        _validate_resume_payload("clarification", {"kind": "clarification", "answers": ["a"]})

    def test_evidence_gap_validated_by_own_schema(self):
        from backend.router.research_router import _validate_resume_payload

        _validate_resume_payload("evidence_gap", {"kind": "evidence_gap", "action": "skip"})
        _validate_resume_payload(
            "evidence_gap",
            {"kind": "evidence_gap", "action": "user_supply", "info": "已知信息"},
        )

    def test_analyze_payload_is_no_longer_judged_as_clarification(self):
        """回归：analyze 的载荷曾按 ClarifyResumePayload 校验，因缺 answers 直接 422。"""
        from backend.router.research_router import _validate_resume_payload

        analyze_payload = {"kind": "evidence_gap", "action": "user_supply", "info": "已知信息"}

        _validate_resume_payload("evidence_gap", analyze_payload)  # 不应抛异常
        with pytest.raises(ValueError):
            _validate_resume_payload("clarification", analyze_payload)

    def test_evidence_gap_user_supply_requires_info(self):
        from backend.router.research_router import _validate_resume_payload

        with pytest.raises(ValueError):
            _validate_resume_payload(
                "evidence_gap", {"kind": "evidence_gap", "action": "user_supply", "info": "   "}
            )

    def test_event_accepts_evidence_gap_kind(self):
        from backend.schemas.events import event

        envelope = event("interrupt.raised", interrupt_id="i1", kind="evidence_gap", payload={})

        assert envelope.data["kind"] == "evidence_gap"

    async def test_analyze_node_raises_evidence_gap_kind(self, monkeypatch):
        from mult_agents.nodes import analyze

        captured = {}

        def fake_raise_interrupt(kind, payload):
            captured["kind"] = kind
            captured["payload"] = payload
            return {"action": "skip"}

        async def fake_invoke(state, prompt, agent, agent_name, node, fallback, writer=None):
            return (
                {
                    "findings": [],
                    "claim_map": [],
                    "needs_more_research": True,
                    "missing_gaps": ["缺口A"],
                    "analysis_summary": "s",
                },
                "",
                [],
            )

        monkeypatch.setattr(analyze, "raise_interrupt", fake_raise_interrupt)
        monkeypatch.setattr(analyze, "_invoke_json_agent", fake_invoke)

        state = {
            "query": "q",
            "hitl_enabled": True,
            "hitl_config": {"analyze_clarify": True},
            "sub_questions": [],
            "evidence_pool": [],
            "audit_flags": [],
        }
        out = await analyze.analyze_node(state, None, "analyst")

        assert captured["kind"] == "evidence_gap"
        assert captured["payload"]["missing_gaps"] == ["缺口A"]
        assert out["needs_more_research"] is False


class TestClarifyAnswerExtraction:
    def test_unwraps_router_validated_dict(self):
        """回归：router 校验后传入的是 dict，节点曾把整段 dict 字符串化成一个「答案」。"""
        from mult_agents.nodes.clarify import _extract_answers

        assert _extract_answers({"kind": "clarification", "answers": ["A", "B"]}) == ["A", "B"]

    def test_accepts_legacy_list_and_scalar(self):
        from mult_agents.nodes.clarify import _extract_answers

        assert _extract_answers(["A", "B"]) == ["A", "B"]
        assert _extract_answers("A") == ["A"]

    def test_missing_answers_falls_back_to_empty(self):
        from mult_agents.nodes.clarify import _extract_answers

        assert _extract_answers({}) == [""]
        assert _extract_answers({"kind": "clarification"}) == [""]
        assert _extract_answers(None) == [""]
