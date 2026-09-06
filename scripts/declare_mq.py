"""通过 RabbitMQ Management HTTP API 声明 DLX/DLQ 和业务队列。

用法: python scripts/declare_mq.py
"""
import base64
import json
import urllib.request

CREDENTIALS = base64.b64encode(b"admin:admin123456").decode()
BASE_URL = "http://localhost:15672/api"


def _put(path: str, payload: dict) -> int:
    url = f"{BASE_URL}/{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="PUT")
    req.add_header("Authorization", f"Basic {CREDENTIALS}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        return resp.status


def declare_all() -> None:
    # 死信交换机
    _put("exchanges/%2F/chunk-sync-dlx", {
        "type": "topic",
        "durable": True,
        "auto_delete": False,
        "internal": False,
        "arguments": {},
    })
    print("Exchange declared: chunk-sync-dlx")

    # 死信队列
    _put("queues/%2F/chunk-sync-dlq", {
        "durable": True,
        "auto_delete": False,
        "arguments": {"x-queue-mode": "lazy"},
    })
    print("Queue declared: chunk-sync-dlq")

    # 绑定 DLQ 到 DLX
    _put("bindings/%2F/e/chunk-sync-dlx/q/chunk-sync-dlq", {
        "routing_key": "chunk.sync.dead",
        "arguments": {},
    })
    print("Binding declared: chunk-sync-dlq -> chunk-sync-dlx [chunk.sync.dead]")

    # 业务队列（带 DLX 参数）
    _put("queues/%2F/chunk-sync.queue", {
        "durable": True,
        "auto_delete": False,
        "arguments": {
            "x-dead-letter-exchange": "chunk-sync-dlx",
            "x-dead-letter-routing-key": "chunk.sync.dead",
        },
    })
    print("Queue declared: chunk-sync.queue (with DLX params)")

    # 业务交换机（如果不存在）
    _put("exchanges/%2F/chunk-sync", {
        "type": "topic",
        "durable": True,
        "auto_delete": False,
        "internal": False,
        "arguments": {},
    })
    print("Exchange declared: chunk-sync")

    # 绑定业务队列到业务交换机
    _put("bindings/%2F/e/chunk-sync/q/chunk-sync.queue", {
        "routing_key": "chunk.sync.#",
        "arguments": {},
    })
    print("Binding declared: chunk-sync.queue -> chunk-sync [chunk.sync.#]")

    print("\nAll RabbitMQ topology declared successfully!")


if __name__ == "__main__":
    declare_all()
