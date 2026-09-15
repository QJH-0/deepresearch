"""
RabbitMQ 消息生产者 — chunk-sync Topic。

参考:
  - RAGFlow: 使用 Redis 任务队列分发文档解析和向量化任务
  - Dify: 使用 Celery + Redis/RabbitMQ 异步处理文档索引
  - 本地消息表 (Outbox Pattern): PG 事务保证消息记录，然后异步发送到 MQ

设计要点:
  1. Topic Exchange: chunk-sync
  2. Queue: chunk-sync.queue (durable, 持久化)
  3. 消息持久化 (delivery_mode=2)，消费者端手动 ACK
  4. 发送失败时本地消息表保留 pending 状态，由补偿任务重试
"""

import json
import logging
from typing import Any, List, Optional

import pika

logger = logging.getLogger("backend.infra.mq")


class MQProducer:
    """RabbitMQ 消息生产者。"""

    def __init__(
        self,
        url: str = "",
        exchange: str = "chunk-sync",
    ):
        self._url = url
        self._exchange = exchange
        self._connection: Optional[pika.BlockingConnection] = None
        self._channel: Optional[Any] = None  # pika.channel.Channel

    def connect(self) -> None:
        """建立 RabbitMQ 连接并声明 exchange + queue（含 DLX 死信拓扑）。

        死信队列是**辅助设施**：声明失败（典型场景是已存在同名队列但参数不同，
        RabbitMQ 会返回 PRECONDITION_FAILED 并关闭 channel）不得挡住宿主队列 —— 
        否则一条陈旧的 DLQ 就能让整条向量化链路静默停摆。
        """
        params = pika.URLParameters(self._url)
        self._connection = pika.BlockingConnection(params)
        self._channel = self._connection.channel()

        self._channel.exchange_declare(
            exchange=self._exchange,
            exchange_type="topic",
            durable=True,
        )

        self._channel.exchange_declare(
            exchange="chunk-sync-dlx",
            exchange_type="topic",
            durable=True,
        )

        try:
            self._channel.queue_declare(
                queue="chunk-sync-dlq",
                durable=True,
                arguments={"x-queue-mode": "lazy"},
            )
            self._channel.queue_bind(
                queue="chunk-sync-dlq",
                exchange="chunk-sync-dlx",
                routing_key="chunk.sync.dead",
            )
        except pika.exceptions.ChannelClosedByBroker as exc:
            logger.error(
                "死信队列 chunk-sync-dlq 声明失败（已存在同名队列但参数不同）: %s\n"
                "  影响：消费失败的消息将无处投递，主链路不受影响\n"
                "  处理：在 RabbitMQ 中删除 chunk-sync-dlq 后重启服务，即按当前参数重建",
                exc,
            )
            # broker 已关闭原 channel，后续声明必须换一个新 channel
            self._channel = self._connection.channel()

        self._channel.queue_declare(
            queue="chunk-sync.queue",
            durable=True,
            arguments={
                "x-dead-letter-exchange": "chunk-sync-dlx",
                "x-dead-letter-routing-key": "chunk.sync.dead",
            },
        )
        self._channel.queue_bind(
            exchange=self._exchange,
            queue="chunk-sync.queue",
            routing_key="chunk.sync.#",
        )

        logger.info(
            "RabbitMQ 连接成功 | exchange=%s | queue=%s",
            self._exchange, "chunk-sync.queue",
        )

    def _ensure_channel(self) -> None:
        """确保 channel 可用，断线重连。"""
        if self._connection is None or self._connection.is_closed:
            self.connect()
        elif self._channel is None or self._channel.is_closed:
            self.connect()

    def publish_chunks(
        self,
        messages: List[dict],
    ) -> bool:
        """
        批量发布 chunk 同步消息到 MQ。

        每条消息:
          - routing_key: chunk.sync.{doc_id}
          - body: JSON payload (包含 chunk_id, content, metadata 等)
          - delivery_mode=2 (持久化)

        返回: True 全部发送成功, False 发送失败
        """
        if not messages:
            return True

        try:
            self._ensure_channel()
            assert self._channel is not None

            for msg in messages:
                payload = msg.get("payload", msg)
                if isinstance(payload, str):
                    payload = json.loads(payload)

                doc_id = payload.get("doc_id", "unknown")
                routing_key = f"chunk.sync.{doc_id}"

                self._channel.basic_publish(
                    exchange=self._exchange,
                    routing_key=routing_key,
                    body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    properties=pika.BasicProperties(
                        delivery_mode=2,  # 持久化消息
                        content_type="application/json",
                        message_id=msg.get("id", ""),
                    ),
                )

            logger.info("MQ 批量发送完成 | count=%d", len(messages))
            return True

        except Exception as exc:
            logger.error("MQ 发送失败: %s", exc)
            return False

    def close(self) -> None:
        """关闭连接。"""
        if self._connection and not self._connection.is_closed:
            try:
                self._connection.close()
            except Exception:
                pass
