"""统一配置入口：pydantic-settings 合并 .env（敏感项）+ config.json（业务项）。

P0 交付物：单一 AppSettings 出口，替代双头解析。
mult_agents/config.py 的 AppConfig 保持类名与字段访问方式不变，内部改为从 AppSettings 取值。
"""

import json
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_ENV_PATH = _PROJECT_ROOT / ".env"
_CONFIG_JSON_PATH = _PROJECT_ROOT / "config.json"


class MiddlewareSettings(BaseSettings):
    """敏感/环境相关配置：.env 优先。"""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_PATH),
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
    )

    dashscope_api_key: str = ""
    postgres_dsn: str = "postgresql://root:postgres123@localhost:5432/mydb"
    redis_url: str = "redis://:redis123456@localhost:6379"
    rabbitmq_url: str = "amqp://admin:admin123456@localhost:5672/"
    milvus_host: str = "localhost"
    milvus_port: int = 19530
    minio_endpoint: str = "localhost:9900"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"


class BusinessSettings(BaseSettings):
    """业务配置：config.json，允许热改。"""

    model_config = SettingsConfigDict(extra="ignore")

    model: str = "qwen-plus"
    tenant_id: str = "default_tenant"
    user_id: str = "default_user"
    thread_id: str = "default"
    max_iterations: int = 3
    enable_memory: bool = True
    # P5: 旧记忆配置已清理，新记忆走 MemoryService
    memory_embedding_model: str = "text-embedding-v3"
    memory_hot_path_top_k: int = 5
    memory_background_enabled: bool = True
    memory_extract_model: str = "qwen-turbo"
    save_conversation_task: bool = False
    checkpointer_backend: str = "postgres"
    enable_milvus: bool = True
    milvus_collection: str = "mult_agent_memory"
    minio_bucket: str = "deep-research-docs"
    minio_secure: bool = False
    rabbitmq_chunk_sync_exchange: str = "chunk-sync"
    hitl_enabled: bool = True
    hitl_config: dict = {
        "plan_review": True,
        "analyze_clarify": True,
        "write_review": False,
    }
    # ── 对话摘要压缩 ──
    summary_threshold: int = 20
    summary_keep_recent: int = 6
    summary_model: str = "qwen-turbo"
    # ── Web 搜索 Provider 链 ──
    search_providers: list = ["ddgs", "searxng"]
    # ── DashScope 专用重排模型 ──
    rerank_model_name: str = "gte-rerank"
    enable_rerank_model: bool = True
    # ── 证据评分 LLM 融合 ──
    evidence_llm_fusion: bool = True
    evidence_prior_weight: float = 0.4

    @model_validator(mode="before")
    @classmethod
    def _load_from_json(cls, values: dict) -> dict:
        """手动加载 config.json（兼容 pydantic-settings < 2.4 不支持 json_file）。"""
        if _CONFIG_JSON_PATH.exists():
            try:
                json_data = json.loads(_CONFIG_JSON_PATH.read_text(encoding="utf-8"))
                if isinstance(json_data, dict):
                    # config.json 的值不覆盖已传入的 values（命令行/环境变量优先）
                    for key, val in json_data.items():
                        if key not in values:
                            values[key] = val
                    # 从 config.json 中删除 api_key（已迁入 .env）
                    values.pop("api_key", None)
            except (json.JSONDecodeError, OSError):
                pass
        return values


class AppSettings(BaseSettings):
    """应用级配置（FastAPI 入口使用）。"""

    app_name: str = "DeepResearch Multi-Agent Assistant"
    app_env: str = "development"
    host: str = "0.0.0.0"
    port: int = 8000
    cors_allow_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    model_config = SettingsConfigDict(
        env_file=str(_ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 延迟加载子配置
    _middleware: MiddlewareSettings | None = None
    _business: BusinessSettings | None = None

    @property
    def middleware(self) -> MiddlewareSettings:
        if self._middleware is None:
            self._middleware = MiddlewareSettings()
        return self._middleware

    @property
    def business(self) -> BusinessSettings:
        if self._business is None:
            self._business = BusinessSettings()
        return self._business

    @property
    def config_path(self) -> str:
        return str(_CONFIG_JSON_PATH)

    def cors_origins(self) -> list[str]:
        values = [item.strip() for item in self.cors_allow_origins.split(",")]
        return [item for item in values if item]

    # ── 便捷属性：直接访问常用配置 ──
    @property
    def dashscope_api_key(self) -> str:
        return self.middleware.dashscope_api_key

    @property
    def postgres_dsn(self) -> str:
        return self.middleware.postgres_dsn

    @property
    def redis_url(self) -> str:
        return self.middleware.redis_url

    @property
    def rabbitmq_url(self) -> str:
        return self.middleware.rabbitmq_url

    @property
    def milvus_host(self) -> str:
        return self.middleware.milvus_host

    @property
    def milvus_port(self) -> int:
        return self.middleware.milvus_port

    @property
    def minio_endpoint(self) -> str:
        return self.middleware.minio_endpoint

    @property
    def minio_access_key(self) -> str:
        return self.middleware.minio_access_key

    @property
    def minio_secret_key(self) -> str:
        return self.middleware.minio_secret_key
