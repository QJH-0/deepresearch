"""2026-09-14 修复批次回归测试。

覆盖审查报告第二篇确认的「宣称可用但实际断链」的四条链路，以及若干工程卫生修复。
每个用例对应一个已修复缺陷，断言写成「失败即代表缺陷复现」的形式。
"""

import json
import pathlib
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from langchain_core.messages import HumanMessage


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


# ──────────────────────────────────────────────────────────────
# P1-3 全局异常处理器不得回吐内部异常详情
# ──────────────────────────────────────────────────────────────


class TestExceptionHandlerHidesInternals:
    def _handler(self):
        import app_main

        for exc_type, handler in app_main.app.exception_handlers.items():
            if exc_type is Exception:
                return handler
        raise AssertionError("未注册 Exception 处理器")

    async def test_body_has_no_exception_text_and_carries_trace_id(self):
        from starlette.requests import Request

        request = Request({
            "type": "http", "method": "GET", "path": "/boom",
            "headers": [], "query_string": b"", "scheme": "http", "root_path": "",
            "server": ("testserver", 80), "client": ("testclient", 1234),
        })
        secret = "postgresql://root:supersecret@db:5432/mydb"
        response = await self._handler()(request, RuntimeError(secret))

        body = json.loads(response.body)

        assert response.status_code == 500
        assert body["detail"] == "Internal Server Error"
        assert body["trace_id"]
        assert "supersecret" not in json.dumps(body)


# ──────────────────────────────────────────────────────────────
# P2-1 摘要消息使用稳定 id，避免长会话累积重复摘要
# ──────────────────────────────────────────────────────────────


class TestConversationSummaryMessageId:
    async def _service(self, monkeypatch):
        from backend.service.summary_service import SummaryService

        svc = SummaryService(api_key="k", threshold=2, keep_recent=1)

        async def fake_generate(messages, existing_summary):
            return "摘要文本"

        monkeypatch.setattr(svc, "_generate_summary", fake_generate)
        return svc

    async def test_summary_message_carries_stable_id(self, monkeypatch):
        from backend.service.summary_service import CONVERSATION_SUMMARY_MESSAGE_ID

        svc = await self._service(monkeypatch)
        msgs = [HumanMessage(content=f"m{i}") for i in range(5)]

        compressed, _ = await svc.summarize_if_needed(msgs, "")

        assert compressed[0].id == CONVERSATION_SUMMARY_MESSAGE_ID

    async def test_repeated_compression_keeps_single_summary_message(self, monkeypatch):
        """回归：无 id 的 SystemMessage 每次压缩都被追加，摘要消息会持续累积。"""
        from langgraph.graph.message import add_messages

        svc = await self._service(monkeypatch)

        state_msgs = []
        for round_no in range(3):
            incoming = [HumanMessage(content=f"r{round_no}-{i}") for i in range(4)]
            compressed, _ = await svc.summarize_if_needed(state_msgs + incoming, "")
            state_msgs = add_messages(state_msgs, compressed)

        summary_count = sum(1 for m in state_msgs if getattr(m, "type", "") == "system")

        assert summary_count == 1, f"摘要消息累积了 {summary_count} 条"


# ──────────────────────────────────────────────────────────────
# P3 语义污染与命名清理
# ──────────────────────────────────────────────────────────────


class TestNodesDoNotPolluteDraft:
    async def test_intent_node_does_not_write_draft(self, monkeypatch):
        """draft 语义是「报告草稿」，路由 JSON 不应写进去。"""
        from mult_agents.nodes import intent

        async def fake_invoke(state, prompt, agent, agent_name, node, fallback, writer=None):
            return {"route": "multiagent", "reason": "r"}, "raw llm text", []

        monkeypatch.setattr(intent, "_invoke_json_agent", fake_invoke)

        out = await intent.intent_node({"query": "q"}, None, "intent_router")

        assert out["intent"] == "multiagent"
        assert "draft" not in out

    async def test_plan_node_does_not_write_draft(self, monkeypatch):
        from mult_agents.nodes import plan

        async def fake_invoke(state, prompt, agent, agent_name, node, fallback, writer=None):
            return (
                {"outline": [], "sub_questions": ["Q1"], "research_questions": [],
                 "budget": {}, "objective": "o"},
                "raw llm text",
                [],
            )

        monkeypatch.setattr(plan, "_invoke_json_agent", fake_invoke)

        out = await plan.plan_node({"query": "q", "hitl_enabled": False}, None, "planner")

        assert "draft" not in out


class TestRagAuxModelNaming:
    def test_aux_llm_model_is_distinct_from_rerank_model_name(self):
        from mult_agents.rag.core import RAGConfig

        cfg = RAGConfig()

        assert cfg.aux_llm_model
        assert cfg.aux_llm_model != cfg.rerank_model_name


class TestRerankFallbackChain:
    def test_falls_back_to_llm_reranker_when_specialized_unavailable(self):
        from mult_agents.rag.core import Document, RAGSystem, RerankUnavailable

        rag = object.__new__(RAGSystem)  # 跳过 __init__，避免连接 Milvus
        rag._reranker_model = MagicMock()
        rag._reranker_model.rerank.side_effect = RerankUnavailable("boom")
        rag._reranker = MagicMock()
        rag._reranker.rerank.return_value = ["llm-result"]

        out = rag._rerank("q", [Document(page_content="a", metadata={})], 3)

        assert out == ["llm-result"]

    def test_uses_llm_reranker_when_no_specialized_model(self):
        from mult_agents.rag.core import Document, RAGSystem

        rag = object.__new__(RAGSystem)
        rag._reranker_model = None
        rag._reranker = MagicMock()
        rag._reranker.rerank.return_value = ["llm-result"]

        out = rag._rerank("q", [Document(page_content="a", metadata={})], 3)

        assert out == ["llm-result"]


# ──────────────────────────────────────────────────────────────
# P1-2 删除文档时同步清理向量与关键词索引
# ──────────────────────────────────────────────────────────────


class TestBm25RemoveDocuments:
    def _retriever(self):
        from mult_agents.rag.core import BM25Retriever, Document

        r = BM25Retriever()
        r.add_documents([
            Document(page_content="alpha beta", metadata={"doc_id": "A"}),
            Document(page_content="alpha gamma", metadata={"doc_id": "A"}),
            Document(page_content="delta epsilon", metadata={"doc_id": "B"}),
        ])
        return r

    def test_removes_only_target_doc_and_rebuilds_stats(self):
        r = self._retriever()

        removed = r.remove_documents("A")

        assert removed == 2
        assert len(r._documents) == 1
        assert r._documents[0].metadata["doc_id"] == "B"
        # df 必须重算：alpha 原只出现在被删文档中，删除后不应残留
        assert "alpha" not in r._df
        assert "delta" in r._df

    def test_unknown_doc_id_is_noop(self):
        r = self._retriever()

        assert r.remove_documents("NOPE") == 0
        assert len(r._documents) == 3


class TestRagDeleteDocumentVectors:
    def _rag(self):
        from mult_agents.rag.core import Document, RAGSystem

        rag = object.__new__(RAGSystem)  # 跳过 __init__，避免连接 Milvus
        rag.vectorstore = MagicMock()
        rag.parent_store = MagicMock()
        rag.bm25 = MagicMock()
        rag.bm25.remove_documents.return_value = 3
        rag._parent_map = {
            "p1": Document(page_content="x", metadata={"doc_id": "docA"}),
            "p2": Document(page_content="y", metadata={"doc_id": "docB"}),
        }
        return rag

    def test_deletes_both_collections_and_evicts_parent_map(self):
        rag = self._rag()

        result = rag.delete_document_vectors("docA")

        assert result["child_deleted"] is True
        assert result["parent_deleted"] is True
        assert result["bm25_removed"] == 3
        assert result["parents_evicted"] == 1
        assert "p1" not in rag._parent_map
        assert "p2" in rag._parent_map
        assert rag.vectorstore.delete.call_args.kwargs["expr"] == 'doc_id == "docA"'

    def test_failure_on_one_path_does_not_abort_the_other(self):
        rag = self._rag()
        rag.vectorstore.delete.side_effect = RuntimeError("milvus down")

        result = rag.delete_document_vectors("docA")

        assert result["child_deleted"] is False
        assert result["parent_deleted"] is True
        assert any("milvus down" in err for err in result["errors"])

    def test_empty_doc_id_is_noop(self):
        rag = self._rag()

        result = rag.delete_document_vectors("")

        rag.vectorstore.delete.assert_not_called()
        assert result["errors"] == []


class TestDocumentServiceVectorCleanup:
    def _service(self, monkeypatch):
        from importlib import import_module

        ds_mod = import_module("backend.service.document_service")
        svc = object.__new__(ds_mod.DocumentService)
        svc._config = None
        svc._repo = MagicMock()
        svc._minio = MagicMock()
        svc._rag = MagicMock()
        svc._mq = None
        svc._initialized = True
        svc._rag.delete_document_vectors.return_value = {"child_deleted": True, "errors": []}
        return svc

    def test_single_delete_cleans_vectors(self, monkeypatch):
        svc = self._service(monkeypatch)
        svc._repo.delete_document.return_value = "obj/key"

        out = svc.delete_document("docA", user_id="u1")

        svc._rag.delete_document_vectors.assert_called_once_with("docA")
        assert out["deleted"] is True
        assert out["vectors"]["child_deleted"] is True

    def test_batch_delete_cleans_vectors_for_each_deleted_doc(self, monkeypatch):
        svc = self._service(monkeypatch)
        svc._repo.delete_documents_batch.return_value = [("d1", "k1"), ("d2", "k2")]

        out = svc.delete_documents_batch(["d1", "d2"], "u1")

        assert out["deleted"] == 2
        called = [c.args[0] for c in svc._rag.delete_document_vectors.call_args_list]
        assert called == ["d1", "d2"]

    def test_missing_document_skips_vector_cleanup(self, monkeypatch):
        svc = self._service(monkeypatch)
        svc._repo.delete_document.return_value = None

        out = svc.delete_document("nope", user_id="u1")

        assert out["deleted"] is False
        svc._rag.delete_document_vectors.assert_not_called()


# ──────────────────────────────────────────────────────────────
# Milvus 动态字段必须显式开启（否则 metadata 被静默丢弃）
# ──────────────────────────────────────────────────────────────


class TestMilvusDynamicFieldEnabled:
    def test_rag_system_enables_dynamic_field_for_both_collections(self, monkeypatch):
        """回归：默认 False 时 langchain_milvus 会从首批 metadata 推断真实列；
        对已存在的集合不再走建表流程，schema 未覆盖的 metadata 键被静默丢弃，
        导致 doc_id / section_path 丢失、引用无法定位、父子扩展失效。
        """
        from mult_agents.rag import core

        captured = []

        def fake_vectorstore(**kwargs):
            captured.append(kwargs)
            return MagicMock()

        monkeypatch.setattr(core, "_MilvusVectorStore", fake_vectorstore)
        monkeypatch.setattr(core.RAGSystem, "_connect_to_milvus", lambda self: None)

        core.RAGSystem(api_key="k", config=core.RAGConfig())

        assert len(captured) == 2, "应为子块与父块各建一个向量库"
        assert all(item["enable_dynamic_field"] is True for item in captured)


class TestParentResolveFallback:
    def _rag(self, query_result):
        from mult_agents.rag.core import RAGSystem

        rag = object.__new__(RAGSystem)  # 跳过 __init__，避免连接 Milvus
        rag._parent_map = {}
        rag.parent_store = MagicMock()
        rag.parent_store.col.query.return_value = query_result
        return rag

    def test_resolves_parent_from_store_on_cache_miss(self):
        """回归：_parent_map 只在写入时填充，进程重启后为空，
        不回查则父子上下文扩展在重启后完全不生效。"""
        rag = self._rag([{
            "text": "父块正文", "doc_id": "d1", "source": "d1",
            "source_name": "手册.md", "section_path": "H1 > H2",
            "parent_id": "p1", "chunk_type": "parent",
        }])

        doc = rag._resolve_parent("p1")

        assert doc is not None
        assert doc.page_content == "父块正文"
        assert doc.metadata["section_path"] == "H1 > H2"
        assert "text" not in doc.metadata, "text 应作为正文而非 metadata"
        assert rag._parent_map["p1"] is doc

    def test_second_call_uses_cache(self):
        rag = self._rag([{"text": "x", "parent_id": "p1"}])

        rag._resolve_parent("p1")
        rag._resolve_parent("p1")

        assert rag.parent_store.col.query.call_count == 1

    def test_returns_none_when_parent_not_found(self):
        rag = self._rag([])

        assert rag._resolve_parent("missing") is None

    def test_returns_none_when_store_has_no_collection(self):
        from mult_agents.rag.core import RAGSystem

        rag = object.__new__(RAGSystem)
        rag._parent_map = {}
        rag.parent_store = MagicMock()
        del rag.parent_store.col  # 取不到底层集合

        assert rag._resolve_parent("p1") is None

    def test_query_failure_is_swallowed(self):
        rag = self._rag([])
        rag.parent_store.col.query.side_effect = RuntimeError("milvus down")

        assert rag._resolve_parent("p1") is None


class TestLocalSourceTitle:
    @pytest.mark.parametrize(
        "metadata,expected",
        [
            ({"source_name": "手册.md", "source": "uuid-1234"}, "手册.md"),
            ({"source": "/data/docs/手册.md"}, "手册.md"),
            ({}, "本地知识片段-3"),
        ],
    )
    def test_title_prefers_filename_over_source_id(self, metadata, expected):
        """回归：source 是文档 id、source_name 才是文件名，
        只取 source 会让引用列表显示一串 uuid。"""
        from mult_agents.rag.core import RAGSystem

        assert RAGSystem._local_title(metadata, 3) == expected


# ──────────────────────────────────────────────────────────────
# P1-4 / P2-10 agent 构建收敛：唯一实现 + collection 单一事实源
# ──────────────────────────────────────────────────────────────


def _minimal_app_config(**overrides):
    from mult_agents.config import AppConfig

    base = dict(
        api_key="test-key",
        model="qwen-plus",
        thread_id="t",
        user_id="u",
        tenant_id="t",
        max_iterations=3,
        enable_memory=False,
        memory_embedding_model="",
        memory_hot_path_top_k=5,
        memory_background_enabled=False,
        memory_extract_model="qwen-turbo",
        save_conversation_task=False,
        checkpointer_backend="memory",
        enable_milvus=False,
        redis_url="",
        postgres_dsn="postgresql://example/db",
        milvus_host="localhost",
        milvus_port=19530,
        milvus_collection="mult_agent_memory",  # 指向一个并不存在的集合
    )
    base.update(overrides)
    return AppConfig(**base)


class TestAgentBuilderConsolidation:
    def test_models_build_agents_uses_shared_collection_constants(self, monkeypatch):
        """回归：生产路径走 models.build_agents，它曾用 config.milvus_collection
        （值为 mult_agent_memory，Milvus 中并不存在），与写入侧集合分叉。"""
        from mult_agents import models
        from mult_agents.rag.core import DEFAULT_CHILD_COLLECTION, DEFAULT_PARENT_COLLECTION

        captured = {}
        monkeypatch.setattr(
            models, "init_rag_system",
            lambda api_key, config: captured.update(cfg=config),
        )
        monkeypatch.setattr(models, "build_agent", lambda *a, **kw: MagicMock())

        config = _minimal_app_config()
        models.build_agents("qwen-plus", "test-key", config)

        cfg = captured["cfg"]
        assert cfg.collection_name == DEFAULT_CHILD_COLLECTION
        assert cfg.parent_collection_name == DEFAULT_PARENT_COLLECTION
        assert cfg.postgres_dsn == config.postgres_dsn

    def test_runtime_no_longer_duplicates_the_builder(self):
        """回归：runtime 与 models 曾各有一份 build_agents，修复只落在一处导致分叉。"""
        from mult_agents import runtime

        assert not hasattr(runtime, "build_agents"), "runtime 不应再重复实现 build_agents"
        assert not hasattr(runtime, "build_agent"), "runtime 不应再重复实现 build_agent"
        assert hasattr(runtime, "AgentBundle"), "AgentBundle 仍由 runtime 提供"

    def test_no_agent_is_built_with_tools(self, monkeypatch):
        """不变量：节点直调函数、不经过 agent tool-calling，因此所有 agent 的 tools 必须为空。

        若将来真的启用工具调用，此断言会失败 —— 那时应同时恢复 tools.py 的 @tool 层。
        """
        from mult_agents import models

        captured_tools = []

        def fake_build_agent(model, api_key, prompt_key, temperature, tools, enable_thinking=False):
            captured_tools.append((prompt_key, tools))
            return MagicMock()

        monkeypatch.setattr(models, "build_agent", fake_build_agent)
        monkeypatch.setattr(models, "init_rag_system", lambda **kw: None)

        models.build_agents("qwen-plus", "test-key", _minimal_app_config())

        assert captured_tools, "未捕获到任何 agent 构建调用"
        assert all(tools == [] for _key, tools in captured_tools), (
            f"存在被绑定工具的 agent: {[k for k, t in captured_tools if t]}"
        )

    def test_tools_module_keeps_no_unwired_tool_layer(self):
        """回归：tools.py 曾保留一层未被任何 agent 绑定的 @tool 定义与占位桩函数。"""
        from mult_agents import tools

        for removed in ("web_search_stub", "amap_weather", "sql_inter",
                        "python_inter", "safe_write_file", "search_knowledge_base"):
            assert not hasattr(tools, removed), f"{removed} 应已随未接线工具层删除"

        for kept in ("web_search_records", "search_knowledge_base_records",
                     "init_rag_system", "SearchProviderChain"):
            assert hasattr(tools, kept), f"{kept} 是仍在使用的入口，不应被删除"

    def test_prompts_do_not_describe_nonexistent_tools(self):
        """回归：prompt 曾告诉 agent 可以使用并不存在的工具，会诱发幻觉调用。"""
        from mult_agents.prompts import PROMPTS

        for key in ("rag_agent", "python_agent", "amap_agent",
                    "file_agent", "sql_agent", "terminal_agent", "web_search_agent"):
            assert key not in PROMPTS, f"{key} 描述的是不存在的工具，应已删除"


# ──────────────────────────────────────────────────────────────
# 流式事件翻译器：两条入口共用一套实现
# ──────────────────────────────────────────────────────────────


class TestStreamTranslator:
    def _translator(self, **kwargs):
        from backend.service.research_service import _StreamTranslator

        return _StreamTranslator("run1", **kwargs)

    def test_token_emits_start_then_delta_once(self):
        t = self._translator()

        first = t.translate("custom", {"type": "token", "node": "write", "text": "A"})
        second = t.translate("custom", {"type": "token", "node": "write", "text": "B"})

        assert sum("message.start" in f for f in first) == 1
        assert sum("message.delta" in f for f in first) == 1
        # 同一节点后续 token 不再重复发 message.start
        assert not any("message.start" in f for f in second)
        assert any("message.delta" in f for f in second)
        assert t.last_token_node == "write"

    def test_thinking_and_sources_and_progress(self):
        t = self._translator()

        assert any("message.thinking" in f for f in t.translate(
            "custom", {"type": "thinking", "node": "analyze", "text": "推理"}))
        assert any("agent.status" in f for f in t.translate(
            "custom", {"type": "progress", "node": "plan"}))
        assert any("sources.found" in f for f in t.translate(
            "custom", {"type": "sources", "sources": [{"title": "t"}]}))

    def test_legacy_node_message_format_still_translated(self):
        t = self._translator()

        frames = t.translate("custom", {"node": "web_search", "message": "检索中"})

        assert any("agent.status" in f for f in frames)

    def test_updates_extracts_final_and_route(self):
        t = self._translator()

        frames = t.translate("updates", {"write": {"final": "报告正文"}})
        t.translate("updates", {"intent": {"intent": "direct"}})

        assert any("agent.status" in f for f in frames)
        assert t.final == "报告正文"
        assert t.route == "direct"

    def test_interrupt_frame_is_emitted_and_stops_processing(self):
        t = self._translator()

        class _Intr:
            id = "i1"
            value = {"kind": "plan_approval"}

        frames = t.translate("updates", {"__interrupt__": [_Intr()]})

        assert len(frames) == 1
        assert "interrupt.raised" in frames[0]
        assert "plan_approval" in frames[0]

    def test_node_completion_is_logged_when_logger_present(self):
        logger = MagicMock()
        t = self._translator(research_logger=logger)

        t.translate("updates", {"plan": {"plan": "x"}})

        logger.log_event.assert_called_once_with("node_complete", {"node": "plan"})

    def test_non_dict_chunk_is_ignored(self):
        t = self._translator()

        assert t.translate("custom", "not a dict") == []
        assert t.translate("updates", None) == []
        assert t.translate("unknown-mode", {}) == []


# ──────────────────────────────────────────────────────────────
# iteration 语义：plan_node 不再重置轮次计数
# ──────────────────────────────────────────────────────────────


class TestIterationSemantics:
    async def test_plan_node_preserves_iteration(self, monkeypatch):
        """回归：plan_node 曾无条件把 iteration 归零，使 write_node 的
        「已达迭代上限」守卫永远无法触发（write 的 +1 会被 plan 覆盖）。"""
        from mult_agents.nodes import plan

        async def fake_invoke(state, prompt, agent, agent_name, node, fallback, writer=None):
            return (
                {"outline": [], "sub_questions": ["Q1"], "research_questions": [],
                 "budget": {}, "objective": "o"},
                "raw",
                [],
            )

        monkeypatch.setattr(plan, "_invoke_json_agent", fake_invoke)

        out = await plan.plan_node(
            {"query": "q", "hitl_enabled": False, "iteration": 2}, None, "planner"
        )

        assert "iteration" not in out, "plan_node 不应重置 iteration"


# ──────────────────────────────────────────────────────────────
# 父子分块：父块内容必须是父块文本，而不是子块副本
# ──────────────────────────────────────────────────────────────


class TestParentChildSplit:
    def _split(self):
        from mult_agents.rag.core import (
            create_parent_splitter,
            create_semantic_chunker,
            split_parent_child,
        )

        markdown_splitter, child_splitter = create_semantic_chunker(chunk_size=200, chunk_overlap=20)
        parent_splitter = create_parent_splitter(parent_chunk_size=600, parent_chunk_overlap=50)
        text = "# 标题\n\n" + "。".join(f"第{i}段内容" * 6 for i in range(30)) + "。"
        return split_parent_child(
            text, "/tmp/手册.md",
            markdown_splitter=markdown_splitter,
            parent_splitter=parent_splitter,
            child_splitter=child_splitter,
        )

    def test_parent_content_is_the_parent_text_not_a_child_copy(self):
        parents, children = self._split()

        assert parents and children
        parent_by_id = {p.metadata["parent_id"]: p for p in parents}
        multi_child_parents = {
            pid for pid in parent_by_id
            if sum(1 for c in children if c.metadata["parent_id"] == pid) > 1
        }
        assert multi_child_parents, "构造的文本应产生至少一个含多个子块的父块"

        for child in children:
            pid = child.metadata["parent_id"]
            if pid not in multi_child_parents:
                continue
            assert child.page_content != parent_by_id[pid].page_content, (
                "父块内容不应等于子块内容"
            )
            assert child.metadata["parent_content"] == parent_by_id[pid].page_content

    def test_children_carry_parent_content_for_async_consumer(self):
        _parents, children = self._split()

        assert all(c.metadata.get("parent_content") for c in children), (
            "子块必须携带 parent_content，否则异步向量化只能把子块内容当父块"
        )

    def test_document_service_uses_the_shared_implementation(self):
        """回归：document_service 曾另抄一份切分逻辑，把父块内容写成了子块内容。"""
        from importlib import import_module

        ds_mod = import_module("backend.service.document_service")
        svc = object.__new__(ds_mod.DocumentService)
        svc._rag = None  # 走本地降级切分器

        chunks = svc._semantic_split("# 标题\n\n" + "内容。" * 200, "/tmp/手册.md", "手册.md")

        assert chunks
        for content, metadata in chunks:
            assert metadata["source_name"] == "手册.md"
            assert metadata.get("parent_content"), "应携带 parent_content"
            assert metadata["chunk_type"] == "child"


class TestChunkConsumerParentContent:
    def _run(self, payload_metadata):
        import json

        from backend.infra.chunk_consumer import _process_message

        rag = MagicMock()
        rag._parent_map = {}
        repo = MagicMock()
        repo.get_chunk_status.return_value = "pending"

        body = json.dumps({
            "chunk_id": "c1", "doc_id": "d1", "content": "子块正文",
            "parent_id": "p1", "section_path": "H1 > H2",
            "source_name": "手册.md", "chunk_idx": 0,
            "metadata": payload_metadata,
        }).encode("utf-8")

        _process_message(rag, repo, body)
        return rag

    def test_uses_parent_content_when_present(self):
        """回归：父块此前用子块 content 构造，父子扩展退化为返回子块副本。"""
        rag = self._run({"child_id": "c1", "parent_content": "父块完整正文"})

        parent_doc = rag.parent_store.add_documents.call_args.args[0][0]
        assert parent_doc.page_content == "父块完整正文"

    def test_falls_back_to_child_content_for_legacy_payload(self):
        rag = self._run({"child_id": "c1"})

        parent_doc = rag.parent_store.add_documents.call_args.args[0][0]
        assert parent_doc.page_content == "子块正文"
