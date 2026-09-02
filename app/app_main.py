"""
DeepResearch FastAPI 应用入口。

启动时:
  1. 初始化 PG 表结构 (documents, document_chunks, chunk_sync_messages)
  2. 启动 RabbitMQ 消费者（后台线程，异步向量化服务）
  3. 注册 REST API 路由

关闭时:
  1. 停止消费者线程
"""
import logging
import logging.handlers
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# ── 在任何库导入之前，禁用系统代理 ──
# Windows 注册表中的代理设置 (ProxyEnable=1) 会导致 dashscope 连接失败
# 必须在 requests/httpx 被导入前设置
os.environ.setdefault("NO_PROXY", "dashscope.aliyuncs.com,localhost,127.0.0.1,0.0.0.0")
os.environ.setdefault("no_proxy", "dashscope.aliyuncs.com,localhost,127.0.0.1,0.0.0.0")
# 清空可能存在的代理环境变量
for _key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_key, None)

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn

from backend.config import AppSettings
from backend.router import health_router, research_router, document_router
from backend.service import init_task_registry, get_task_registry, init_memory_service, get_memory_service
from backend.infra import init_store, close_store, get_store
from mult_agents.config import AppConfig
from mult_agents.rag.core import RAGConfig
from mult_agents.tools import init_rag_system


# ── 统一日志配置：控制台 + 文件（按天轮转，保留 7 天）
_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_log_dir = Path(__file__).resolve().parents[1] / "output" / "logs"
_log_dir.mkdir(parents=True, exist_ok=True)

# 创建 root logger handler
_root_logger = logging.getLogger()
_root_logger.setLevel(logging.INFO)

# 控制台 handler
_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
_root_logger.addHandler(_console_handler)

# 文件 handler — 按大小轮转（10MB × 5 个备份）
_file_handler = logging.handlers.RotatingFileHandler(
    filename=_log_dir / "deepresearch.log",
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
_root_logger.addHandler(_file_handler)

# TRACE 级别关键日志单独落到一个文件，方便排障
_trace_file_handler = logging.handlers.RotatingFileHandler(
    filename=_log_dir / "trace.log",
    maxBytes=10 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
_trace_file_handler.setLevel(logging.INFO)
_trace_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
_trace_file_handler.addFilter(lambda record: "[TRACE]" in record.getMessage())
_root_logger.addHandler(_trace_file_handler)

logging.getLogger("mult_agents").setLevel(logging.INFO)
logging.getLogger("backend").setLevel(logging.INFO)
# uvicorn access 接入 root logger，控制台也能看到请求
logging.getLogger("uvicorn").setLevel(logging.INFO)
logging.getLogger("uvicorn.access").setLevel(logging.INFO)

logger = logging.getLogger("app_main")
logger.info("日志已初始化 ➜ %s", _log_dir)

# 全局消费者引用（用于 shutdown）
_chunk_consumer = None


def _init_infra() -> None:
    """初始化基础设施: PG 表 + RAG + MQ 消费者。"""
    global _chunk_consumer

    settings = AppSettings()
    config = AppConfig.from_file(settings.config_path)

    # 1. 初始化 PG 表结构
    from backend.infra.postgres_client import ensure_tables
    ensure_tables(config.postgres_dsn)

    # 2. 初始化 RAG 系统
    rag_config = RAGConfig(
        milvus_host=config.milvus_host,
        milvus_port=config.milvus_port,
        collection_name="mult_agent_knowledge",
        parent_collection_name="mult_agent_knowledge_parent",
    )
    rag_init_ok = True
    try:
        init_rag_system(api_key=config.api_key, config=rag_config)
    except RuntimeError as exc:
        logger.error("RAG 系统初始化失败: %s", exc)
        rag_init_ok = False

    # 3. 启动 MQ 消费者（即使 RAG 失败也尝试启动，消费者内部会重试 RAG 初始化）
    from backend.infra.chunk_consumer import ChunkSyncConsumer
    rabbitmq_url = getattr(config, 'rabbitmq_url', 'amqp://admin:admin123456@localhost:5672/')
    rabbitmq_exchange = getattr(config, 'rabbitmq_chunk_sync_exchange', 'chunk-sync')
    _chunk_consumer = ChunkSyncConsumer(
        mq_url=rabbitmq_url,
        exchange=rabbitmq_exchange,
        dsn=config.postgres_dsn,
        api_key=config.api_key,
        rag_config=rag_config,
    )
    try:
        _chunk_consumer.start()
        logger.info("MQ 消费者已启动")
    except Exception as exc:
        logger.warning("MQ 消费者启动失败（向量化将降级为同步模式）: %s", exc)
        _chunk_consumer = None

    if rag_init_ok:
        logger.info("基础设施初始化完成: PG 表 + RAG + MQ 消费者")
    else:
        logger.warning("基础设施初始化部分完成: RAG 不可用，文档上传后将无法向量化")


async def _init_task_registry_and_scan(config: AppConfig) -> None:
    """初始化 TaskRegistry 并执行崩溃恢复扫描。

    P3-2: 进程重启后扫 Redis 拘留的 running 标记 + PG checkpointer，
    标记 interrupted_by_restart，前端可据此提示"研究已中断可继续"。
    """
    redis_client = None
    try:
        import redis.asyncio as aioredis
        redis_client = aioredis.from_url(config.redis_url, decode_responses=True)
        await redis_client.ping()
        logger.info("Redis 连接成功，TaskRegistry 启用多 worker 兜底")
    except Exception as exc:
        logger.warning("Redis 连接失败，TaskRegistry 降级为单进程模式: %s", exc)
        redis_client = None

    registry = init_task_registry(redis=redis_client)

    # 崩溃恢复扫描：需要 graph_app 来检查 PG checkpoint
    # 延迟导入避免循环
    try:
        from mult_agents.models import build_agents
        from mult_agents.graph import build_app as build_workflow_app
        from mult_agents.runtime import build_checkpointer

        agents = build_agents(config.model, config.api_key, config)
        checkpointer = build_checkpointer(config)
        graph_app = build_workflow_app(agents, checkpointer)

        orphaned = await registry.scan_orphans(graph_app=graph_app)
        if orphaned:
            logger.info("崩溃恢复扫描完成，标记 %d 个中断会话: %s", len(orphaned), orphaned)
        else:
            logger.info("崩溃恢复扫描完成，无中断会话")
    except Exception as exc:
        logger.warning("崩溃恢复扫描失败（不阻塞启动）: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan: 启动和关闭钩子。"""
    # 启动
    _init_infra()

    # P3-2: 初始化 TaskRegistry + 崩溃恢复扫描
    settings = AppSettings()
    config = AppConfig.from_file(settings.config_path)
    await _init_task_registry_and_scan(config)

    # P5: 初始化 PostgresStore + MemoryService
    try:
        await init_store(
            postgres_dsn=config.postgres_dsn,
            dashscope_api_key=config.api_key,
        )
        init_memory_service(
            api_key=config.api_key,
            model="qwen-turbo",
            hot_path_top_k=5,
            background_enabled=True,
        )
        logger.info("P5 记忆系统初始化完成 (PostgresStore + langmem)")
    except Exception as exc:
        logger.warning("P5 记忆系统初始化失败（不阻塞启动）: %s", exc)

    yield
    # 关闭
    # P5: 等待后台记忆任务完成 + 关闭 PostgresStore
    mem_service = get_memory_service()
    if mem_service is not None:
        try:
            await mem_service.await_background_tasks()
        except Exception:
            pass
    try:
        await close_store()
    except Exception:
        pass

    global _chunk_consumer
    if _chunk_consumer is not None:
        _chunk_consumer.stop()
        logger.info("MQ 消费者已停止")

    # 关闭 Redis 连接
    registry = get_task_registry()
    if registry.redis is not None:
        try:
            await registry.redis.aclose()
            logger.info("Redis 连接已关闭")
        except Exception:
            pass


def create_app() -> FastAPI:
    settings = AppSettings()
    app = FastAPI(
        title=settings.app_name,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health_router)
    app.include_router(research_router)
    app.include_router(document_router)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled exception on %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(
            status_code=500,
            content={"detail": f"Internal Server Error: {exc}"},
        )

    return app


app = create_app()


if __name__ == "__main__":
    runtime_settings = AppSettings()
    uvicorn.run(
        "app_main:app",
        host=runtime_settings.host,
        port=runtime_settings.port,
        reload=runtime_settings.app_env == "development",
    )
