"""
RabbitMQ 消费者 — chunk-sync 异步向量化服务。

参考:
  - RAGFlow: task_executor 消费 Redis 队列中的文档解析和 embedding 任务
  - Dify: Celery worker 异步执行 chunk embedding + index 任务
  - pika BasicConsumer + manual_ack: 消费失败不 ACK，消息重回队列

异步链路流程:
  1. 消费者拉取 chunk-sync.queue 消息
  2. 调用 Embedding 模型生成向量
  3. 写入 Milvus (子块 + 父块)
  4. 更新 PG chunk vector_status = 'indexed'
  5. 手动 ACK 消息

容错策略:
  - Embedding 或 Milvus 写入失败 → NACK (requeue=False，进入死信或丢弃)
  - PG 更新失败 → 消息已 ACK（向量已写入），下次启动时补偿扫描
  - 消费者启动时自动扫描 pending 消息补偿发送
"""

import json
import logging
import threading
import time
from typing import Any, Optional

import pika

from mult_agents.config import AppConfig
from mult_agents.rag.core import RAGSystem, RAGConfig
import mult_agents.tools as _tools_mod
from mult_agents.tools import init_rag_system
from .postgres_client import ChunkRepository

logger = logging.getLogger("backend.infra.consumer")

# ── MQ 消息回调 ──────────────────────────────────────────────────────

def _create_chunk_sync_callback(
    rag: RAGSystem,
    repo: ChunkRepository,
):
    """
    创建 pika 消费回调函数。

    回调流程:
      1. 解析消息 payload (chunk_id, content, parent_id, metadata, ...)
      2. 调用 RAG 系统将 chunk 写入 Milvus (子块向量 + 父块向量)
      3. 更新 PG 中 chunk 的 vector_status = 'indexed'
      4. 手动 ACK
    """

    def callback(
        ch: pika.channel.Channel,
        method: pika.spec.Basic.Deliver,
        properties: pika.spec.BasicProperties,
        body: bytes,
    ) -> None:
        delivery_tag = method.delivery_tag
        try:
            payload = json.loads(body.decode("utf-8"))
            chunk_id = payload.get("chunk_id", "")
            content = payload.get("content", "")
            parent_id = payload.get("parent_id", "")
            section_path = payload.get("section_path", "")
            source_name = payload.get("source_name", "unknown")
            doc_id = payload.get("doc_id", "")

            logger.info(
                "消费消息 | chunk_id=%s | doc_id=%s | content_len=%d",
                chunk_id, doc_id, len(content),
            )

            # Step 1: 调用 Embedding 生成向量并写入 Milvus
            # 复用 RAG 系统的 ingest 逻辑，但只处理单个 chunk
            from langchain_core.documents import Document

            # 从原始 metadata 中提取 child_id（Milvus schema 要求该字段非空）
            raw_metadata = payload.get("metadata", {})
            child_id = raw_metadata.get("child_id", "")

            # 写入子块向量库
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

            # 同步更新 BM25 索引
            rag.bm25.add_documents([child_doc])

            # 写入父块向量库（如存在 parent_id）
            if parent_id and parent_id not in rag._parent_map:
                parent_doc = Document(
                    page_content=content,  # 父块内容 = 当前 chunk 内容（简化版）
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

            # Step 2: 更新 PG chunk 状态
            repo.update_chunk_vector_status(
                chunk_id=chunk_id,
                status="indexed",
                milvus_pk="",
            )

            # Step 3: 手动 ACK
            ch.basic_ack(delivery_tag=delivery_tag)
            logger.info("ACK 完成 | chunk_id=%s", chunk_id)

        except Exception as exc:
            logger.error(
                "消费失败 | delivery_tag=%s | error=%s",
                delivery_tag, exc,
                exc_info=True,
            )
            # NACK + requeue=False: 避免无限循环重试
            # 实际生产中应配置死信队列 (DLX) 接收失败消息
            ch.basic_nack(delivery_tag=delivery_tag, requeue=False)

    return callback


class ChunkSyncConsumer:
    """chunk-sync MQ 消费者，后台线程运行。"""

    def __init__(
        self,
        mq_url: str,
        exchange: str,
        dsn: str,
        api_key: str,
        rag_config: RAGConfig,
    ):
        self._mq_url = mq_url
        self._exchange = exchange
        self._dsn = dsn
        self._api_key = api_key
        self._rag_config = rag_config
        self._rag: Optional[RAGSystem] = None
        self._repo: Optional[ChunkRepository] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def _init_dependencies(self) -> None:
        """初始化 RAG 系统和 PG 仓库。"""
        # 初始化 RAG 系统（复用全局单例）
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

        # 启动时补偿扫描：将 PG 中 pending 的本地消息重新发送到 MQ
        self._compensate_pending_messages()

        params = pika.URLParameters(self._mq_url)
        connection = pika.BlockingConnection(params)
        channel = connection.channel()

        # 声明 exchange + queue（幂等）
        channel.exchange_declare(
            exchange=self._exchange,
            exchange_type="topic",
            durable=True,
        )
        channel.queue_declare(
            queue="chunk-sync.queue",
            durable=True,
        )
        channel.queue_bind(
            exchange=self._exchange,
            queue="chunk-sync.queue",
            routing_key="chunk.sync.#",
        )

        # prefetch_count=1: 每次只分发给消费者一条消息，处理完再下一条
        channel.basic_qos(prefetch_count=1)

        # 设置消费者
        callback = _create_chunk_sync_callback(
            rag=self._rag,
            repo=self._repo,
        )
        channel.basic_consume(
            queue="chunk-sync.queue",
            on_message_callback=callback,
            auto_ack=False,  # 手动 ACK
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

        这解决了"PG 事务已提交但 MQ 发送失败"的场景。
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
