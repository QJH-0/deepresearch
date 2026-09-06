"""
RabbitMQ 消费者 — chunk-sync 异步向量化服务。

异步链路流程:
  1. 消费者拉取 chunk-sync.queue 消息
  2. 幂等检查：该 chunk 已 indexed → 直接 ACK 跳过
  3. 调用 Embedding 模型生成向量并写入 Milvus
  4. 更新 PG chunk vector_status = 'indexed'
  5. 手动 ACK 消息

容错策略:
  - 处理失败按消息体 hash 累计重试次数
  - 重试超限 → nack(requeue=False) → DLX 路由进死信队列 (DLQ)
  - 消费幂等：处理前检查 chunk 状态，indexed 直接 ACK 跳过
  - 消费者启动时自动扫描 pending outbox 消息补偿发送
"""

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Optional

import pika

from mult_agents.config import AppConfig
from mult_agents.rag.core import RAGSystem, RAGConfig
import mult_agents.tools as _tools_mod
from mult_agents.tools import init_rag_system
from .postgres_client import ChunkRepository

logger = logging.getLogger("backend.infra.consumer")

# ── 常量 ─────────────────────────────────────────────────────────────

_DEFAULT_RETRY_LIMIT = 3
_BACKOFF_MAX = 10
_RETRY_CACHE_MAX = 1000


# ── MQ 消息回调 ──────────────────────────────────────────────────────

def _body_hash(body: bytes) -> str:
    """计算消息体 md5，用作重试计数 key。"""
    return hashlib.md5(body).hexdigest()


def _create_chunk_sync_callback(
    rag: RAGSystem,
    repo: ChunkRepository,
    retry_limit: int = _DEFAULT_RETRY_LIMIT,
    retry_counts: Optional[OrderedDict] = None,
):
    """
    创建 pika 消费回调函数。

    回调流程:
      1. 幂等检查：chunk 已 indexed → ACK 跳过
      2. 向量化写入 Milvus + BM25
      3. 更新 PG chunk 状态为 indexed
      4. 手动 ACK
      失败时按 body hash 累计重试，超限 nack(requeue=False) → DLQ
    """

    if retry_counts is None:
        retry_counts = OrderedDict()

    def callback(
        ch: pika.channel.Channel,
        method: pika.spec.Basic.Deliver,
        properties: pika.spec.BasicProperties,
        body: bytes,
    ) -> None:
        delivery_tag = method.delivery_tag
        bhash = _body_hash(body)

        try:
            _process_message(rag, repo, body)
            ch.basic_ack(delivery_tag=delivery_tag)
            retry_counts.pop(bhash, None)

        except Exception as exc:
            count = retry_counts.get(bhash, 0) + 1
            retry_counts[bhash] = count

            if len(retry_counts) > _RETRY_CACHE_MAX:
                retry_counts.popitem(last=False)

            if count >= retry_limit:
                logger.error(
                    "[chunk-consumer] 重试超限进 DLQ | delivery=%s | body_hash=%s | body=%s | error=%s",
                    delivery_tag, bhash[:8], body[:200], exc,
                )
                retry_counts.pop(bhash, None)
                ch.basic_nack(delivery_tag=delivery_tag, requeue=False)
            else:
                logger.warning(
                    "[chunk-consumer] 处理失败(%d/%d) | body_hash=%s | error=%s",
                    count, retry_limit, bhash[:8], exc,
                )
                backoff = min(count * 2, _BACKOFF_MAX)
                time.sleep(backoff)
                ch.basic_nack(delivery_tag=delivery_tag, requeue=True)

    return callback


def _process_message(rag: RAGSystem, repo: ChunkRepository, body: bytes) -> None:
    """
    解析消息并执行向量化，含幂等闸门。

    幂等检查：chunk 已 indexed → 直接返回（外层 ACK）
    状态回写失败视为整条处理失败（抛异常走重试路径），
    保证「成功状态 ⇒ 已向量化」不变式。
    """
    payload = json.loads(body.decode("utf-8"))
    chunk_id = payload.get("chunk_id", "")
    content = payload.get("content", "")
    parent_id = payload.get("parent_id", "")
    section_path = payload.get("section_path", "")
    source_name = payload.get("source_name", "unknown")
    doc_id = payload.get("doc_id", "")

    status = repo.get_chunk_status(chunk_id)
    if status == "indexed":
        logger.info("[chunk-consumer] chunk 已向量化，幂等跳过 | chunk=%s", chunk_id)
        return

    logger.info(
        "消费消息 | chunk_id=%s | doc_id=%s | content_len=%d",
        chunk_id, doc_id, len(content),
    )

    from langchain_core.documents import Document

    raw_metadata = payload.get("metadata", {})
    child_id = raw_metadata.get("child_id", "")

    child_doc = Document(
        page_content=content,
        metadata={
            "source": doc_id,
            "source_name": source_name,
            "section_path": section_path,
            "parent_id": parent_id,
            "child_id": child_id,
            "chunk_type": "child",
            "chunk_idx": payload.get("chunk_idx", 0),
            "doc_id": doc_id,
        },
    )
    rag.vectorstore.add_documents([child_doc])
    rag.bm25.add_documents([child_doc])

    if parent_id and parent_id not in rag._parent_map:
        parent_doc = Document(
            page_content=content,
            metadata={
                "source": doc_id,
                "source_name": source_name,
                "section_path": section_path,
                "parent_id": parent_id,
                "chunk_type": "parent",
                "doc_id": doc_id,
            },
        )
        rag.parent_store.add_documents([parent_doc])
        rag._parent_map[parent_id] = parent_doc

    logger.info("Milvus 写入成功 | chunk_id=%s", chunk_id)

    repo.update_chunk_vector_status(
        chunk_id=chunk_id,
        status="indexed",
        milvus_pk="",
    )


class ChunkSyncConsumer:
    """chunk-sync MQ 消费者，后台线程运行。"""

    def __init__(
        self,
        mq_url: str,
        exchange: str,
        dsn: str,
        api_key: str,
        rag_config: RAGConfig,
        retry_limit: int = _DEFAULT_RETRY_LIMIT,
    ):
        self._mq_url = mq_url
        self._exchange = exchange
        self._dsn = dsn
        self._api_key = api_key
        self._rag_config = rag_config
        self._retry_limit = retry_limit
        self._rag: Optional[RAGSystem] = None
        self._repo: Optional[ChunkRepository] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._retry_counts: OrderedDict = OrderedDict()

    def _init_dependencies(self) -> None:
        """初始化 RAG 系统和 PG 仓库。"""
        if _tools_mod._RAG_SYSTEM is None:
            try:
                init_rag_system(api_key=self._api_key, config=self._rag_config)
            except RuntimeError as exc:
                raise RuntimeError(
                    f"RAG 系统初始化失败，请检查 Milvus ({self._rag_config.milvus_host}:{self._rag_config.milvus_port}) "
                    f"和 API Key 配置: {exc}"
                ) from exc
        self._rag = _tools_mod._RAG_SYSTEM
        if self._rag is None:
            raise RuntimeError("RAG 系统初始化失败：_RAG_SYSTEM 为 None，请检查日志")

        self._repo = ChunkRepository(dsn=self._dsn)

    def _run(self) -> None:
        """消费者主循环。"""
        try:
            self._init_dependencies()
        except Exception as exc:
            logger.error("消费者依赖初始化失败: %s", exc)
            return

        self._compensate_pending_messages()

        params = pika.URLParameters(self._mq_url)
        connection = pika.BlockingConnection(params)
        channel = connection.channel()

        channel.exchange_declare(
            exchange=self._exchange,
            exchange_type="topic",
            durable=True,
        )

        channel.exchange_declare(
            exchange="chunk-sync-dlx",
            exchange_type="topic",
            durable=True,
        )
        channel.queue_declare(
            queue="chunk-sync-dlq",
            durable=True,
            arguments={"x-queue-mode": "lazy"},
        )
        channel.queue_bind(
            queue="chunk-sync-dlq",
            exchange="chunk-sync-dlx",
            routing_key="chunk.sync.dead",
        )
        channel.queue_declare(
            queue="chunk-sync.queue",
            durable=True,
            arguments={
                "x-dead-letter-exchange": "chunk-sync-dlx",
                "x-dead-letter-routing-key": "chunk.sync.dead",
            },
        )
        channel.queue_bind(
            exchange=self._exchange,
            queue="chunk-sync.queue",
            routing_key="chunk.sync.#",
        )

        channel.basic_qos(prefetch_count=1)

        callback = _create_chunk_sync_callback(
            rag=self._rag,
            repo=self._repo,
            retry_limit=self._retry_limit,
            retry_counts=self._retry_counts,
        )
        channel.basic_consume(
            queue="chunk-sync.queue",
            on_message_callback=callback,
            auto_ack=False,
        )

        logger.info("ChunkSyncConsumer 启动，等待 chunk-sync 消息...")
        while self._running:
            connection.process_data_events(time_limit=1)

        channel.stop_consuming()
        connection.close()
        logger.info("ChunkSyncConsumer 已停止")

    def _compensate_pending_messages(self) -> None:
        """
        启动时扫描 PG 本地消息表中的 pending 消息，重新发送到 MQ。

        与 DLQ 重放不冲突：补偿扫的是 PG outbox 表 pending 记录，
        DLQ 重放扫的是 RabbitMQ 死信队列中的消费失败消息。
        """
        if self._repo is None:
            return
        from .mq_client import MQProducer

        pending = self._repo.get_pending_messages(limit=500)
        if not pending:
            logger.info("补偿扫描: 无 pending 消息")
            return

        logger.info("补偿扫描: 发现 %d 条 pending 消息，重新发送", len(pending))
        producer = MQProducer(url=self._mq_url, exchange=self._exchange)
        try:
            producer.connect()
            for msg in pending:
                ok = producer.publish_chunks([msg])
                if ok:
                    self._repo.mark_messages_sent([msg["id"]])
        finally:
            producer.close()

    def start(self) -> None:
        """后台线程启动消费者。"""
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            name="ChunkSyncConsumer",
            daemon=True,
        )
        self._thread.start()
        logger.info("ChunkSyncConsumer 线程已启动")

    def stop(self) -> None:
        """停止消费者。"""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info("ChunkSyncConsumer 已请求停止")
