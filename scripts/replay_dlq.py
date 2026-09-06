"""DLQ 人工重放脚本 — 将死信队列中的消息重新投回业务队列。

用法:
    python scripts/replay_dlq.py [--limit N] [--dry-run]

行为:
    1. 从 chunk-sync-dlq 取（basic_get）最多 N 条消息（默认全部）
    2. --dry-run：只打印 doc_id/chunk_id 与首次失败原因，不重投
    3. 正式模式：以原 routing key 重新 publish 到 chunk-sync 交换机，
       publish 成功后 ACK DLQ 中的该消息
    4. 结束打印：重放 N 条、成功 M 条、失败 K 条
"""

import argparse
import json
import logging
import os
import sys

import pika

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("replay_dlq")

_BUSINESS_EXCHANGE = "chunk-sync"
_DLQ_QUEUE = "chunk-sync-dlq"


def _parse_death_headers(properties: pika.spec.BasicProperties) -> list:
    """从 x-death 头提取失败原因列表。"""
    if not properties or not properties.headers:
        return []
    x_death = properties.headers.get("x-death", [])
    return x_death if isinstance(x_death, list) else []


def replay_dlq(
    mq_url: str,
    limit: int = 0,
    dry_run: bool = False,
) -> dict:
    """
    从 DLQ 取消息重投到业务队列。

    返回统计: {replayed, succeeded, failed, dry_run}
    """
    params = pika.URLParameters(mq_url)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()

    stats = {"replayed": 0, "succeeded": 0, "failed": 0, "dry_run": dry_run}

    while True:
        if limit and stats["replayed"] >= limit:
            break

        method, properties, body = channel.basic_get(queue=_DLQ_QUEUE, auto_ack=False)
        if method is None:
            break

        stats["replayed"] += 1

        try:
            payload = json.loads(body.decode("utf-8"))
            doc_id = payload.get("doc_id", "unknown")
            chunk_id = payload.get("chunk_id", "unknown")
            deaths = _parse_death_headers(properties)
            first_reason = deaths[0].get("reason", "unknown") if deaths else "unknown"

            logger.info(
                "DLQ 消息 #%d | doc_id=%s | chunk_id=%s | reason=%s",
                stats["replayed"], doc_id, chunk_id, first_reason,
            )

            if dry_run:
                channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                continue

            routing_key = f"chunk.sync.{doc_id}"
            channel.basic_publish(
                exchange=_BUSINESS_EXCHANGE,
                routing_key=routing_key,
                body=body,
                properties=pika.BasicProperties(
                    delivery_mode=2,
                    content_type="application/json",
                ),
            )
            channel.basic_ack(delivery_tag=method.delivery_tag)
            stats["succeeded"] += 1
            logger.info("重投成功 | doc_id=%s | chunk_id=%s", doc_id, chunk_id)

        except Exception as exc:
            logger.error("重投失败 | error=%s", exc)
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            stats["failed"] += 1

    connection.close()
    logger.info(
        "DLQ 重放完成 | 重放=%d | 成功=%d | 失败=%d | dry_run=%s",
        stats["replayed"], stats["succeeded"], stats["failed"], dry_run,
    )
    return stats


def main():
    parser = argparse.ArgumentParser(description="DLQ 人工重放")
    parser.add_argument("--limit", type=int, default=0, help="最多重放 N 条（0=全部）")
    parser.add_argument("--dry-run", action="store_true", help="只打印不重投")
    args = parser.parse_args()

    mq_url = os.environ.get(
        "RABBITMQ_URL",
        "amqp://admin:admin123456@localhost:5672/",
    )
    replay_dlq(mq_url, limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
