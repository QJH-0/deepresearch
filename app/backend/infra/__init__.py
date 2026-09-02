"""基础设施层：MinIO 对象存储、PostgreSQL 事务管理、RabbitMQ 消息队列。"""

from .minio_client import MinIOStorage
from .postgres_client import (
    ChunkRepository,
    ThreadRepository,
    ensure_tables,
    generate_thread_title,
    DocumentRecord,
    ChunkRecord,
)
from .mq_client import MQProducer
from .chunk_consumer import ChunkSyncConsumer

__all__ = [
    "MinIOStorage",
    "ChunkRepository",
    "ThreadRepository",
    "ensure_tables",
    "generate_thread_title",
    "DocumentRecord",
    "ChunkRecord",
    "MQProducer",
    "ChunkSyncConsumer",
]
