"""运行时基础设施：Agent 构建、checkpointer 初始化、记忆管理器构建。

从旧 main.py 迁移，供 workflow_service / eval_metrics 等模块调用。
P0 阶段保持行为不变，仅迁移位置。
"""

import importlib
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from langchain_community.chat_models import ChatTongyi
from langchain.agents import create_agent
from langgraph.checkpoint.memory import InMemorySaver

from .config import AppConfig
from .prompts import PROMPTS
from .tools import init_rag_system
from .rag.core import RAGConfig

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "mult_agents"

logger = logging.getLogger("mult_agents")

# 全局记忆管理器引用（CLI 模式使用）
MEMORY_MANAGER: Optional["MemoryManager"] = None  # noqa: F821 — 前向引用，P5 将删除
CHECKPOINTER_CONTEXT = None


def colorize(text: str, color: str) -> str:
    """终端彩色输出辅助（从旧 main.py 迁移）。"""
    if os.getenv("NO_COLOR"):
        return text
    ANSI = {
        "reset": "\033[0m",
        "cyan": "\033[36m",
        "magenta": "\033[35m",
        "yellow": "\033[33m",
        "green": "\033[32m",
        "red": "\033[31m",
    }
    code = ANSI.get(color, "")
    if not code:
        return text
    return f"{code}{text}{ANSI['reset']}"


@dataclass(frozen=True)
class AgentBundle:
    """全部节点的 Agent 集合。"""

    intent_router: any
    planner: any
    scout_web: any
    scout_local: any
    evidence_judge: any
    analyst: any
    direct_responder: any
    writer: any


def build_agent(model: str, api_key: str, prompt_key: str, temperature: float, tools: list):
    """构建单个 Agent（ChatTongyi + prompt + tools）。"""
    if api_key:
        os.environ["DASHSCOPE_API_KEY"] = api_key
    llm = ChatTongyi(model=model, temperature=temperature)
    prompt = PROMPTS[prompt_key]
    return create_agent(model=llm, tools=tools, system_prompt=prompt)


def build_agents(model: str, api_key: str, config: AppConfig) -> AgentBundle:
    """构建全部节点 Agent。"""
    rag_config = RAGConfig(
        milvus_host=config.milvus_host,
        milvus_port=config.milvus_port,
        collection_name=config.milvus_collection,
    )
    init_rag_system(api_key=api_key, config=rag_config)
    return AgentBundle(
        intent_router=build_agent(model, api_key, "intent_router", 0.0, []),
        planner=build_agent(model, api_key, "plan", 0.3, []),
        scout_web=build_agent(model, api_key, "web_search", 0.4, []),
        scout_local=build_agent(model, api_key, "local_rag", 0.4, []),
        evidence_judge=build_agent(model, api_key, "deep_dive", 0.2, []),
        analyst=build_agent(model, api_key, "analyze", 0.3, []),
        direct_responder=build_agent(model, api_key, "direct_answer", 0.2, []),
        writer=build_agent(model, api_key, "write", 0.4, []),
    )


def build_memory_manager(config: AppConfig) -> Optional["MemoryManager"]:  # noqa: F821
    """构建记忆管理器（P5 将重写为 langmem，当前保持旧实现）。"""
    if not config.enable_memory:
        return None
    try:
        from .memory import MemoryManager  # noqa: F811 — 延迟导入避免循环

        return MemoryManager(
            short_term_ttl=config.short_term_ttl_seconds,
            short_term_max_messages=config.short_term_max_messages,
            short_term_summary_threshold=config.short_term_summary_threshold,
            tenant_id=config.tenant_id,
            short_term_backend=config.short_term_backend,
            long_term_backend=config.long_term_backend,
            long_term_scope=config.long_term_scope,
            save_conversation_task=config.save_conversation_task,
            enable_milvus=config.enable_milvus,
            redis_url=config.redis_url,
            postgres_dsn=config.postgres_dsn,
            milvus_host=config.milvus_host,
            milvus_port=config.milvus_port,
            milvus_collection=config.milvus_collection,
            embedding_api_key=config.api_key,
        )
    except Exception as exc:
        logger.exception("初始化 MemoryManager 失败，已禁用外部记忆: %s", exc)
        return None


def build_checkpointer(config: AppConfig):
    """构建 checkpointer（PG → Redis → 内存 降级链）。"""
    global CHECKPOINTER_CONTEXT
    backend = config.checkpointer_backend
    if backend in {"postgres", "auto"} and config.enable_memory and config.postgres_dsn:
        postgres_saver = None
        postgres_import_error = ""
        try:
            module = importlib.import_module("langgraph.checkpoint.postgres")
            postgres_saver = getattr(module, "PostgresSaver", None)
        except Exception as exc:
            postgres_import_error = str(exc)
        if postgres_saver is None:
            try:
                module = importlib.import_module("langgraph_checkpoint_postgres")
                postgres_saver = getattr(module, "PostgresSaver", None)
            except Exception as exc:
                postgres_import_error = postgres_import_error or str(exc)
        if postgres_saver is None:
            message = (
                "PostgreSQL checkpointer 模块不可用。请安装: pip install langgraph-checkpoint-postgres "
                f"| import_error={postgres_import_error or 'unknown'}"
            )
            if backend == "postgres":
                logger.warning("%s %s", colorize("[memory]", "yellow"), message)
            else:
                logger.info("%s %s", colorize("[memory]", "cyan"), message)
        else:
            try:
                CHECKPOINTER_CONTEXT = postgres_saver.from_conn_string(config.postgres_dsn)
                checkpointer = CHECKPOINTER_CONTEXT.__enter__()
                checkpointer.setup()
                logger.info("%s 使用 PostgreSQL checkpointer", colorize("[memory]", "green"))
                return checkpointer
            except Exception as exc:
                logger.warning("%s PostgreSQL checkpointer 初始化失败: %s", colorize("[memory]", "yellow"), exc)
    if backend in {"redis", "auto"} and config.enable_memory and config.redis_url:
        from langgraph.checkpoint.redis import RedisSaver

        candidate_urls = [config.redis_url]
        if "redis://root:" in config.redis_url:
            candidate_urls.append(config.redis_url.replace("redis://root:", "redis://:"))
        last_exc = None
        for url in candidate_urls:
            try:
                CHECKPOINTER_CONTEXT = RedisSaver.from_conn_string(url)
                checkpointer = CHECKPOINTER_CONTEXT.__enter__()
                checkpointer.setup()
                logger.info("%s 使用 Redis checkpointer", colorize("[memory]", "green"))
                return checkpointer
            except Exception as exc:
                last_exc = exc
        if last_exc and "FT._LIST" in str(last_exc):
            logger.warning(
                "%s Redis checkpointer 依赖 RediSearch(FT._LIST)。当前 Redis 非 Redis Stack，已降级。",
                colorize("[memory]", "yellow"),
            )
        else:
            logger.warning("%s Redis checkpointer 初始化失败，降级内存: %s", colorize("[memory]", "yellow"), last_exc)
    if backend == "memory":
        logger.info("%s 使用内存 checkpointer", colorize("[memory]", "green"))
    return InMemorySaver()
