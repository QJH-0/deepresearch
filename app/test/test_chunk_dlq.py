"""R3.1 DLQ 死信队列与消费幂等 — 单元测试。

覆盖: DLX 队列声明参数、重试计数（body hash key）、超限进 DLQ、
      幂等跳过已索引 chunk、状态回写失败视为处理失败、DLQ 重放脚本。
"""

import json
import hashlib
from collections import OrderedDict
from unittest.mock import MagicMock, patch, call

import pytest


# ── T3.1-01 DLX 队列声明参数 ──────────────────────────────────────

class TestDLXDeclaration:
    """验证生产者 connect() 声明了 DLX 交换机、DLQ 队列与业务队列 DLX 参数。"""

    def test_dlx_declaration_in_producer(self):
        """MQProducer.connect() 声明 DLX/DLQ 并给业务队列绑定 DLX 参数。"""
        from backend.infra.mq_client import MQProducer

        producer = MQProducer(url="amqp://test", exchange="chunk-sync")

        with patch("backend.infra.mq_client.pika") as mock_pika:
            mock_conn = MagicMock()
            mock_channel = MagicMock()
            mock_pika.BlockingConnection.return_value = mock_conn
            mock_conn.channel.return_value = mock_channel
            producer.connect()

        declare_calls = mock_channel.queue_declare.call_args_list
        assert len(declare_calls) >= 2, "应至少声明业务队列 + DLQ 两个队列"

        dlq_found = False
        biz_found = False
        for dc in declare_calls:
            qname = dc.kwargs.get("queue") or (dc.args[0] if dc.args else None)
            args_dict = dc.kwargs.get("arguments", {})
            if qname == "chunk-sync-dlq":
                dlq_found = True
                assert args_dict.get("x-queue-mode") == "lazy"
            elif qname == "chunk-sync.queue":
                biz_found = True
                assert args_dict.get("x-dead-letter-exchange") == "chunk-sync-dlx"
                assert args_dict.get("x-dead-letter-routing-key") == "chunk.sync.dead"

        assert dlq_found, "DLQ 队列应被声明"
        assert biz_found, "业务队列应带 DLX 参数"

        exchange_declares = mock_channel.exchange_declare.call_args_list
        ex_names = [dc.kwargs.get("exchange") for dc in exchange_declares]
        assert "chunk-sync-dlx" in ex_names, "DLX 交换机应被声明"

        bind_calls = mock_channel.queue_bind.call_args_list
        dlq_bind_found = any(
            (bc.kwargs.get("queue") == "chunk-sync-dlq" and
             bc.kwargs.get("routing_key") == "chunk.sync.dead")
            for bc in bind_calls
        )
        assert dlq_bind_found, "DLQ 应绑定到 DLX 的 chunk.sync.dead 路由键"


# ── T3.1-02 ~ T3.1-05 重试计数与 DLQ 路由 ──────────────────────────

class TestRetryAndDLQ:
    """验证失败重试计数以 body hash 为 key，超限后 nack(requeue=False) 进 DLQ。"""

    def _make_callback(self, retry_limit=3, retry_counts=None):
        from backend.infra.chunk_consumer import _create_chunk_sync_callback

        rag = MagicMock()
        repo = MagicMock()
        repo.get_chunk_status.return_value = "pending"

        if retry_counts is None:
            retry_counts = OrderedDict()

        callback = _create_chunk_sync_callback(
            rag=rag, repo=repo,
            retry_limit=retry_limit,
            retry_counts=retry_counts,
        )
        return callback, rag, repo, retry_counts

    def _make_body(self, chunk_id="c1", content="hello"):
        return json.dumps({
            "chunk_id": chunk_id,
            "doc_id": "d1",
            "content": content,
            "parent_id": "",
            "section_path": "",
            "metadata": {},
            "source_name": "test.md",
        }).encode("utf-8")

    def test_first_failure_requeue(self):
        """T3.1-02: 首次失败 nack(requeue=True)。"""
        callback, rag, repo, rc = self._make_callback(retry_limit=3)
        ch = MagicMock()
        method = MagicMock(delivery_tag=1)
        body = self._make_body()

        rag.vectorstore.add_documents.side_effect = RuntimeError("Milvus down")

        callback(ch, method, None, body)

        ch.basic_nack.assert_called_once_with(delivery_tag=1, requeue=True)
        ch.basic_ack.assert_not_called()
        assert len(rc) == 1

    def test_retry_limit_enters_dlq(self):
        """T3.1-03: 第 3 次失败 nack(requeue=False) 进 DLQ，计数清理。"""
        callback, rag, repo, rc = self._make_callback(retry_limit=3)
        ch = MagicMock()
        body = self._make_body()

        rag.vectorstore.add_documents.side_effect = RuntimeError("persistent fail")

        for i in range(3):
            method = MagicMock(delivery_tag=i + 1)
            callback(ch, method, None, body)

        final_nack = ch.basic_nack.call_args_list[-1]
        assert final_nack.kwargs.get("requeue") is False
        assert len(rc) == 0, "超限后计数应清理"

    def test_count_key_is_body_hash_not_delivery_tag(self):
        """T3.1-04: delivery_tag 变化但 body 相同 → 计数累加。"""
        callback, rag, repo, rc = self._make_callback(retry_limit=3)
        ch = MagicMock()
        body = self._make_body()

        rag.vectorstore.add_documents.side_effect = RuntimeError("fail")

        method1 = MagicMock(delivery_tag=5)
        callback(ch, method1, None, body)
        method2 = MagicMock(delivery_tag=9)
        callback(ch, method2, None, body)

        from backend.infra.chunk_consumer import _body_hash
        bhash = _body_hash(body)
        assert rc[bhash] == 2, "第二次失败时计数应为 2"

    def test_success_clears_count(self):
        """T3.1-05: 成功后计数清零，再次失败从 1 开始。"""
        callback, rag, repo, rc = self._make_callback(retry_limit=3)
        ch = MagicMock()
        body = self._make_body()

        rag.vectorstore.add_documents.side_effect = RuntimeError("fail")
        method1 = MagicMock(delivery_tag=1)
        callback(ch, method1, None, body)
        callback(ch, MagicMock(delivery_tag=2), None, body)
        assert len(rc) == 1

        rag.vectorstore.add_documents.side_effect = None
        callback(ch, MagicMock(delivery_tag=3), None, body)
        ch.basic_ack.assert_called_once()
        assert len(rc) == 0

        rag.vectorstore.add_documents.side_effect = RuntimeError("fail again")
        callback(ch, MagicMock(delivery_tag=4), None, body)
        from backend.infra.chunk_consumer import _body_hash
        assert rc[_body_hash(body)] == 1


# ── T3.1-06 ~ T3.1-08 幂等闸门 ─────────────────────────────────────

class TestIdempotency:
    """验证消费幂等：已 indexed 的 chunk 直接跳过。"""

    def _make_callback(self, repo=None):
        from backend.infra.chunk_consumer import _create_chunk_sync_callback

        rag = MagicMock()
        if repo is None:
            repo = MagicMock()
            repo.get_chunk_status.return_value = "pending"

        rc = OrderedDict()
        callback = _create_chunk_sync_callback(
            rag=rag, repo=repo, retry_limit=3, retry_counts=rc,
        )
        return callback, rag, repo, rc

    def _make_body(self, chunk_id="c1"):
        return json.dumps({
            "chunk_id": chunk_id, "doc_id": "d1", "content": "hello",
            "parent_id": "", "section_path": "", "metadata": {},
            "source_name": "test.md",
        }).encode("utf-8")

    def test_idempotent_skip_indexed(self):
        """T3.1-06: chunk 已 indexed → 向量化未调用，正常 ACK。"""
        callback, rag, repo, rc = self._make_callback()
        repo.get_chunk_status.return_value = "indexed"

        ch = MagicMock()
        method = MagicMock(delivery_tag=1)
        body = self._make_body()

        callback(ch, method, None, body)

        ch.basic_ack.assert_called_once()
        rag.vectorstore.add_documents.assert_not_called()

    def test_pending_not_blocked(self):
        """T3.1-07: chunk 状态 pending → 走完整向量化。"""
        callback, rag, repo, rc = self._make_callback()
        repo.get_chunk_status.return_value = "pending"

        ch = MagicMock()
        method = MagicMock(delivery_tag=1)
        body = self._make_body()

        callback(ch, method, None, body)

        ch.basic_ack.assert_called_once()
        rag.vectorstore.add_documents.assert_called_once()
        repo.update_chunk_vector_status.assert_called_once_with(
            chunk_id="c1", status="indexed", milvus_pk="",
        )

    def test_status_writeback_failure_triggers_retry(self):
        """T3.1-08: 向量化成功但状态回写失败 → 整体抛异常进重试。"""
        callback, rag, repo, rc = self._make_callback()
        repo.get_chunk_status.return_value = "pending"
        repo.update_chunk_vector_status.side_effect = RuntimeError("PG down")

        ch = MagicMock()
        method = MagicMock(delivery_tag=1)
        body = self._make_body()

        callback(ch, method, None, body)

        ch.basic_ack.assert_not_called()
        ch.basic_nack.assert_called_once_with(delivery_tag=1, requeue=True)


# ── T3.1-09 DLQ 重放脚本 ───────────────────────────────────────────

class TestReplayDLQ:
    """验证 DLQ 重放脚本核心函数。"""

    def test_replay_dry_run(self):
        """--dry-run 模式：只打印不 publish，消息不 ACK。"""
        from scripts.replay_dlq import replay_dlq

        bodies = [
            (MagicMock(delivery_tag=i + 1), MagicMock(headers={}),
             json.dumps({"doc_id": f"d{i}", "chunk_id": f"c{i}"}).encode())
            for i in range(2)
        ]
        # basic_get 依次返回 2 条消息，第三次返回 (None, None, None) 表示空
        bodies.append((None, None, None))

        with patch("scripts.replay_dlq.pika") as mock_pika:
            mock_conn = MagicMock()
            mock_channel = MagicMock()
            mock_pika.BlockingConnection.return_value = mock_conn
            mock_conn.channel.return_value = mock_channel
            mock_channel.basic_get.side_effect = bodies

            stats = replay_dlq("amqp://test", limit=0, dry_run=True)

        assert stats["replayed"] == 2
        assert stats["succeeded"] == 0
        assert stats["dry_run"] is True
        mock_channel.basic_publish.assert_not_called()

    def test_replay_normal(self):
        """正式模式：消息以原 routing key 重发，成功后 ACK。"""
        from scripts.replay_dlq import replay_dlq

        bodies = [
            (MagicMock(delivery_tag=1), MagicMock(headers={}),
             json.dumps({"doc_id": "d1", "chunk_id": "c1"}).encode()),
        ]
        bodies.append((None, None, None))

        with patch("scripts.replay_dlq.pika") as mock_pika:
            mock_conn = MagicMock()
            mock_channel = MagicMock()
            mock_pika.BlockingConnection.return_value = mock_conn
            mock_conn.channel.return_value = mock_channel
            mock_channel.basic_get.side_effect = bodies

            stats = replay_dlq("amqp://test", dry_run=False)

        assert stats["replayed"] == 1
        assert stats["succeeded"] == 1
        mock_channel.basic_publish.assert_called_once()
        pub_call = mock_channel.basic_publish.call_args
        assert pub_call.kwargs.get("routing_key") == "chunk.sync.d1"
        mock_channel.basic_ack.assert_called_once_with(delivery_tag=1)
