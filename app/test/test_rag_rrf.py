"""阶段一 R1.2 测试：RRF 融合 + PG 关键词检索。

覆盖用例:
    T1.2-01~04 rrf_fuse 纯函数
    T1.2-05~06 PostgresKeywordRetriever
    T1.2-07~08 search_records 双路融合接线

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    python -m pytest app/test/test_rag_rrf.py -v
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
sys.path.insert(0, str(_APP_PATH))


# ──────────────────────────────────────────────
# T1.2-01~04 rrf_fuse 纯函数
# ──────────────────────────────────────────────


class TestRRFFuse:
    """rrf_fuse 融合算法测试。"""

    def test_rrf_basic_fusion(self):
        """T1.2-01 RRF 基本融合排名【P0】"""
        from mult_agents.rag.core import rrf_fuse, Document

        d1 = Document(page_content="d1")
        d2 = Document(page_content="d2")
        d3 = Document(page_content="d3")
        d4 = Document(page_content="d4")

        A = [d1, d2, d3]
        B = [d2, d1, d4]
        result = rrf_fuse([A, B], k=60)

        # d1 (rank1+rank2) 与 d2 (rank2+rank1) 总分相等且排最前
        top2_contents = {result[0].page_content, result[1].page_content}
        assert top2_contents == {"d1", "d2"}
        # d3、d4 次之
        assert result[2].page_content in {"d3", "d4"}
        assert result[3].page_content in {"d3", "d4"}
        # 无重复
        assert len(result) == 4
        assert len(set(r.page_content for r in result)) == 4

    def test_rrf_single_list(self):
        """T1.2-02 RRF 单路输入【P1】"""
        from mult_agents.rag.core import rrf_fuse, Document

        d1 = Document(page_content="d1")
        d2 = Document(page_content="d2")
        result = rrf_fuse([[d1, d2]])
        assert [r.page_content for r in result] == ["d1", "d2"]

    def test_rrf_cross_list_merge(self):
        """T1.2-03 RRF 同文档跨路合并【P0】"""
        from mult_agents.rag.core import rrf_fuse, Document

        d_a = Document(page_content="same content")
        d_b = Document(page_content="same content")
        A = [d_a]
        B = [d_b]
        result = rrf_fuse([A, B])
        assert len(result) == 1
        assert result[0].page_content == "same content"

    def test_rrf_top_k_and_empty(self):
        """T1.2-04 RRF top_k 截断与空输入【P1】"""
        from mult_agents.rag.core import rrf_fuse, Document

        docs_a = [Document(page_content=f"d{i}") for i in range(3)]
        docs_b = [Document(page_content=f"d{i+2}") for i in range(3)]
        result = rrf_fuse([docs_a, docs_b], top_k=3)
        assert len(result) == 3
        assert rrf_fuse([]) == []


# ──────────────────────────────────────────────
# T1.2-05~06 PostgresKeywordRetriever
# ──────────────────────────────────────────────


class TestPostgresKeywordRetriever:
    """PostgresKeywordRetriever 测试。"""

    def test_pg_normal_search(self):
        """T1.2-05 PostgresKeywordRetriever 正常检索【P0】"""
        import sys
        import psycopg2 as psycopg2_mod  # 触发 mock 注册到 sys.modules
        from mult_agents.rag.core import PostgresKeywordRetriever

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            ("cid1", "content1", "doc1", "pid1", "sec")
        ]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.close = MagicMock()

        psycopg2_mod.connect = MagicMock(return_value=mock_conn)

        retriever = PostgresKeywordRetriever("dsn://localhost")
        result = retriever.search("q", k=5)

        assert len(result) == 1
        doc = result[0]
        assert doc.metadata["parent_id"] == "pid1"
        assert doc.metadata["doc_id"] == "doc1"
        assert doc.metadata["section_path"] == "sec"
        assert doc.metadata["chunk_id"] == "cid1"
        assert doc.page_content == "content1"

        call_args = mock_cursor.execute.call_args
        sql = call_args[0][0]
        assert "plainto_tsquery" in sql
        assert "ts_rank" in sql

    def test_pg_connection_failure_degrade(self):
        """T1.2-06 PostgresKeywordRetriever 连接失败降级【P0】"""
        import psycopg2 as psycopg2_mod
        from mult_agents.rag.core import PostgresKeywordRetriever

        psycopg2_mod.connect = MagicMock(side_effect=Exception("connection refused"))

        retriever = PostgresKeywordRetriever("dsn://localhost")
        result = retriever.search("q")
        assert result == []

    def test_pg_no_dsn_unavailable(self):
        """PG 无 DSN 不可用。"""
        from mult_agents.rag.core import PostgresKeywordRetriever

        retriever = PostgresKeywordRetriever("")
        assert retriever.available is False
        assert retriever.search("q") == []


# ──────────────────────────────────────────────
# T1.2-07~08 search_records 双路融合接线
# ──────────────────────────────────────────────


class TestSearchRecordsFusion:
    """search_records 双路融合接线测试。"""

    def test_search_records_uses_rrf_fuse(self):
        """T1.2-07 search_records 双路融合接线【P0】"""
        import sys
        from mult_agents.rag.core import rrf_fuse, Document, RAGSystem

        # mock pymilvus.utility.has_collection
        pymilvus_util = sys.modules.get("pymilvus")
        if pymilvus_util is not None:
            if not hasattr(pymilvus_util, 'utility'):
                pymilvus_util.utility = MagicMock()
            pymilvus_util.utility.has_collection = MagicMock(return_value=True)

        mock_rag = MagicMock()
        mock_rag.config.enable_query_rewrite = False
        mock_rag.config.enable_bm25 = True
        mock_rag.config.enable_reranker = False
        mock_rag.config.enable_parent_child = False
        mock_rag.config.recall_k = 5
        mock_rag.config.rrf_k = 60

        d1 = Document(page_content="vec1", metadata={"parent_id": "", "source": "s1"})
        d2 = Document(page_content="vec2", metadata={"parent_id": "", "source": "s2"})

        d2_b = Document(page_content="vec2", metadata={"parent_id": "", "source": "s2"})
        d3 = Document(page_content="kw1", metadata={"parent_id": "", "source": "s3"})

        mock_rag.vectorstore = MagicMock()
        mock_rag.vectorstore.similarity_search = MagicMock(return_value=[d1, d2])
        mock_rag.bm25 = MagicMock()
        mock_rag.bm25._documents = [d2_b, d3]
        mock_rag.bm25.search = MagicMock(return_value=[(d2_b, 1.0), (d3, 0.5)])
        mock_rag._keyword_retriever = None

        original_search_records = RAGSystem.search_records
        result = original_search_records(mock_rag, "query", k=2)

        assert len(result) <= 2
        snippets = [r["snippet"] for r in result]
        assert "vec2" in snippets

    def test_pg_unavailable_degrades_to_bm25(self):
        """T1.2-08 PG 不可用降级 BM25【P0】"""
        import sys
        from mult_agents.rag.core import Document, RAGSystem

        pymilvus_util = sys.modules.get("pymilvus")
        if pymilvus_util is not None:
            if not hasattr(pymilvus_util, 'utility'):
                pymilvus_util.utility = MagicMock()
            pymilvus_util.utility.has_collection = MagicMock(return_value=True)

        mock_rag = MagicMock()
        mock_rag.config.enable_query_rewrite = False
        mock_rag.config.enable_bm25 = True
        mock_rag.config.enable_reranker = False
        mock_rag.config.enable_parent_child = False
        mock_rag.config.recall_k = 5
        mock_rag.config.rrf_k = 60

        d1 = Document(page_content="vec1", metadata={"parent_id": "", "source": "s1"})
        d2 = Document(page_content="bm1", metadata={"parent_id": "", "source": "s2"})

        mock_rag.vectorstore = MagicMock()
        mock_rag.vectorstore.similarity_search = MagicMock(return_value=[d1])
        mock_rag.bm25 = MagicMock()
        mock_rag.bm25._documents = [d2]
        mock_rag.bm25.search = MagicMock(return_value=[(d2, 1.0)])

        mock_pg = MagicMock()
        mock_pg.available = True
        mock_pg.search = MagicMock(side_effect=Exception("PG down"))
        mock_rag._keyword_retriever = mock_pg

        original_search_records = RAGSystem.search_records
        result = original_search_records(mock_rag, "query", k=2)

        assert len(result) >= 1
        snippets = [r["snippet"] for r in result]
        assert "bm1" in snippets
