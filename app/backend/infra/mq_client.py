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
        url: str = "amqp://admin:admin123456@localhost:5672/",
        exchange: str = "chunk-sync",
    ):
        self._url = url
        self._exchange = exchange
        self._connection: Optional[pika.BlockingConnection] = None
        self._channel: Optional[Any] = None  # pika.channel.Channel

    def connect(self) -> None:
        """建立 RabbitMQ 连接并声明 exchange + queue。"""
        params = pika.URLParameters(self._url)
        self._connection = pika.BlockingConnection(params)
        self._channel = self._connection.channel()

        # 声明 Topic Exchange（持久化）
        self._channel.exchange_declare(
            exchange=self._exchange,
            exchange_type="topic",
            durable=True,
        )

        # 声明持久化 Queue
        self._channel.queue_declare(
            queue="chunk-sync.queue",
            durable=True,
        )

        # 绑定: chunk-sync exchange → chunk-sync.queue
        self._channel.queue_bind(
            exchange=self._exchange,
            queue="chunk-sync.queue",
            routing_key="chunk.sync.#",
        )

        logger.info(
            "RabbitMQ 连接成功 | exchange=%s | queue=%s | url=%s",
            self._exchange, "chunk-sync.queue", self._url.replace(
                self._url.split("@")[0].split("//")[1] + ":",
                "***@",
            ),
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
