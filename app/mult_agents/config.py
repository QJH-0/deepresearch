"""配置模块：统一加载 .env 与 config.json，并构建全局 AppConfig。

P0 改造：内部委托给 backend.config.settings 的 pydantic-settings AppSettings，
保持 AppConfig 类名与字段访问方式不变，避免 P0 大范围改调用点。
"""

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import load_dotenv

# 加载项目根目录的 .env 文件
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_PATH = _PROJECT_ROOT / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)

# 清除代理环境变量，避免 Windows 系统代理导致 dashscope 连接失败
for _key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    _val = os.getenv(_key, "")
    if _val == "":
        os.environ.pop(_key, None)


@dataclass(frozen=True)
class AppConfig:
    """全局配置（兼容层：字段不变，内部从 pydantic-settings 取值）。"""

    api_key: str
    model: str
    thread_id: str
    user_id: str
    tenant_id: str
    max_iterations: int
    enable_memory: bool
    # P5: 新记忆配置
    memory_embedding_model: str
    memory_hot_path_top_k: int
    memory_background_enabled: bool
    memory_extract_model: str
    save_conversation_task: bool
    checkpointer_backend: str
    enable_milvus: bool
    redis_url: str
    postgres_dsn: str
    milvus_host: str
    milvus_port: int
    milvus_collection: str
    # ── MinIO 对象存储 ──
    minio_endpoint: str = "localhost:9900"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "deep-research-docs"
    minio_secure: bool = False
    # ── RabbitMQ 消息队列 ──
    rabbitmq_url: str = "amqp://admin:admin123456@localhost:5672/"
    rabbitmq_chunk_sync_exchange: str = "chunk-sync"
    # ── HITL 配置 ──
    hitl_enabled: bool = False
    hitl_config: dict = field(default_factory=lambda: {
        "plan_review": True,
        "analyze_clarify": True,
        "write_review": False,
    })

    def with_overrides(self, **kwargs) -> "AppConfig":
        cleaned = {k: v for k, v in kwargs.items() if v is not None}
        return replace(self, **cleaned)

    @staticmethod
    def _default_config_path() -> Path:
        return _PROJECT_ROOT / "config.json"

    @staticmethod
    def from_file(path: str | Path | None = None) -> "AppConfig":
        """从 .env + config.json 构建 AppConfig（通过 pydantic-settings）。"""
        from backend.config.settings import MiddlewareSettings, BusinessSettings

        mw = MiddlewareSettings()
        biz = BusinessSettings()

        # 环境变量覆盖 config.json 的值
        def _env_str(key: str, default: str = "") -> str:
            val = os.getenv(key)
            return val.strip() if val and val.strip() else default

        def _env_int(key: str, default: int) -> int:
            val = os.getenv(key)
            if val and val.strip():
                try:
                    return int(val.strip())
                except ValueError:
                    pass
            return default

        def _env_bool(key: str, default: bool) -> bool:
            val = os.getenv(key)
            if val and val.strip():
                return val.strip().lower() == "true"
            return default

        # api_key 从 .env (DASHSCOPE_API_KEY) 读取，不从 config.json
        api_key = mw.dashscope_api_key or os.getenv("DASHSCOPE_API_KEY", "")
        if not api_key:
            raise ValueError(
                "缺少 DASHSCOPE_API_KEY 配置，请在 .env 中设置 DASHSCOPE_API_KEY"
            )

        return AppConfig(
            api_key=api_key,
            model=_env_str("MODEL", biz.model),
            thread_id=_env_str("THREAD_ID", biz.thread_id),
            user_id=_env_str("USER_ID", biz.user_id),
            tenant_id=_env_str("TENANT_ID", biz.tenant_id),
            max_iterations=_env_int("MAX_ITERATIONS", biz.max_iterations),
            enable_memory=_env_bool("ENABLE_MEMORY", biz.enable_memory),
            # P5: 新记忆配置
            memory_embedding_model=_env_str("MEMORY_EMBEDDING_MODEL", biz.memory_embedding_model),
            memory_hot_path_top_k=_env_int("MEMORY_HOT_PATH_TOP_K", biz.memory_hot_path_top_k),
            memory_background_enabled=_env_bool("MEMORY_BACKGROUND_ENABLED", biz.memory_background_enabled),
            memory_extract_model=_env_str("MEMORY_EXTRACT_MODEL", biz.memory_extract_model),
            save_conversation_task=_env_bool("SAVE_CONVERSATION_TASK", biz.save_conversation_task),
            checkpointer_backend=_env_str("CHECKPOINTER_BACKEND", biz.checkpointer_backend).lower(),
            enable_milvus=_env_bool("ENABLE_MILVUS", biz.enable_milvus),
            redis_url=_env_str("REDIS_URL", mw.redis_url),
            postgres_dsn=_env_str("POSTGRES_DSN", mw.postgres_dsn),
            milvus_host=_env_str("MILVUS_HOST", mw.milvus_host),
            milvus_port=_env_int("MILVUS_PORT", mw.milvus_port),
            milvus_collection=_env_str("MILVUS_COLLECTION", biz.milvus_collection),
            minio_endpoint=_env_str("MINIO_ENDPOINT", mw.minio_endpoint),
            minio_access_key=_env_str("MINIO_ACCESS_KEY", mw.minio_access_key),
            minio_secret_key=_env_str("MINIO_SECRET_KEY", mw.minio_secret_key),
            minio_bucket=_env_str("MINIO_BUCKET", biz.minio_bucket),
            minio_secure=_env_bool("MINIO_SECURE", biz.minio_secure),
            rabbitmq_url=_env_str("RABBITMQ_URL", mw.rabbitmq_url),
            rabbitmq_chunk_sync_exchange=_env_str("RABBITMQ_CHUNK_SYNC_EXCHANGE", biz.rabbitmq_chunk_sync_exchange),
            hitl_enabled=_env_bool("HITL_ENABLED", biz.hitl_enabled),
            hitl_config=biz.hitl_config,
        )

    @staticmethod
    def from_env() -> "AppConfig":
        """从纯环境变量构建（不读 config.json）。"""
        return AppConfig.from_file(None)
