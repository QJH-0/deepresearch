"""运行时基础设施：Agent 构建、checkpointer 初始化、记忆管理器构建。

从旧 main.py 迁移，供 workflow_service / eval_metrics 等模块调用。
P0 阶段保持行为不变，仅迁移位置。
"""

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from .config import AppConfig

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "mult_agents"

logger = logging.getLogger("mult_agents")

# P5: MEMORY_MANAGER 全局引用已删除，记忆由 MemoryService (langmem + PostgresStore) 管理
# P2-2: checkpointer 异步单例（lifespan 初始化，生产 astream/ainvoke 路径复用）
_checkpointer_instance = None
_checkpointer_context = None


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
    reflector: any
    scout_web: any
    scout_local: any
    retrieval_grader: any
    evidence_judge: any
    analyst: any
    direct_responder: any
    writer: any
    clarifier: any


async def init_checkpointer(config: AppConfig):
    """异步初始化 checkpointer（生产 astream/ainvoke 路径，lifespan 调用）。

    降级链：Postgres → Redis → 内存。
    生产执行链是 astream（异步），必须用 AsyncPostgresSaver / AsyncRedisSaver；
    sync PostgresSaver 无 aget_tuple/aput 方法，graph.astream 会落到基类 stub 抛
    NotImplementedError。
    """
    global _checkpointer_instance, _checkpointer_context
    if _checkpointer_instance is not None:
        return _checkpointer_instance

    backend = config.checkpointer_backend

    if backend in {"postgres", "auto"} and config.enable_memory and config.postgres_dsn:
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            _checkpointer_context = AsyncPostgresSaver.from_conn_string(config.postgres_dsn)
            _checkpointer_instance = await _checkpointer_context.__aenter__()
            await _checkpointer_instance.setup()
            logger.info("%s 使用 PostgreSQL checkpointer（异步）", colorize("[memory]", "green"))
            return _checkpointer_instance
        except Exception as exc:
            logger.warning("%s AsyncPostgresSaver 初始化失败，尝试下一级: %s",
                           colorize("[memory]", "yellow"), exc)
            _checkpointer_instance = None
            _checkpointer_context = None

    if backend in {"redis", "auto"} and config.enable_memory and config.redis_url:
        try:
            from langgraph.checkpoint.redis import AsyncRedisSaver

            _checkpointer_context = AsyncRedisSaver.from_conn_string(config.redis_url)
            _checkpointer_instance = await _checkpointer_context.__aenter__()
            await _checkpointer_instance.setup()
            logger.info("%s 使用 Redis checkpointer（异步）", colorize("[memory]", "green"))
            return _checkpointer_instance
        except Exception as exc:
            logger.warning("%s AsyncRedisSaver 初始化失败，降级内存: %s",
                           colorize("[memory]", "yellow"), exc)
            _checkpointer_instance = None
            _checkpointer_context = None

    logger.info("%s 使用内存 checkpointer", colorize("[memory]", "green"))
    _checkpointer_instance = InMemorySaver()
    return _checkpointer_instance


async def close_checkpointer() -> None:
    """关闭 checkpointer 连接（lifespan shutdown 调用）。"""
    global _checkpointer_instance, _checkpointer_context
    if _checkpointer_context is not None:
        try:
            await _checkpointer_context.__aexit__(None, None, None)
            logger.info("%s checkpointer 连接已关闭", colorize("[memory]", "cyan"))
        except Exception as exc:
            logger.warning("%s checkpointer 关闭失败: %s", colorize("[memory]", "yellow"), exc)
    _checkpointer_instance = None
    _checkpointer_context = None


def get_checkpointer():
    """获取已初始化的 checkpointer 单例（未初始化返回 None）。"""
    return _checkpointer_instance


def build_checkpointer(config: AppConfig):
    """同步 checkpointer 工厂（供 sync 执行场景，如 eval_metrics 的 graph.invoke）。

    生产 astream/ainvoke 路径请用 init_checkpointer()；本函数只服务同步 invoke 场景，
    因此选用 sync PostgresSaver / RedisSaver（无需 async 方法）。
    """
    backend = config.checkpointer_backend
    if backend in {"postgres", "auto"} and config.enable_memory and config.postgres_dsn:
        try:
            from langgraph.checkpoint.postgres import PostgresSaver

            ctx = PostgresSaver.from_conn_string(config.postgres_dsn)
            checkpointer = ctx.__enter__()
            checkpointer.setup()
            logger.info("%s 使用 PostgreSQL checkpointer（同步）", colorize("[memory]", "green"))
            return checkpointer
        except Exception as exc:
            logger.warning("%s 同步 PostgresSaver 初始化失败，尝试下一级: %s",
                           colorize("[memory]", "yellow"), exc)
    if backend in {"redis", "auto"} and config.enable_memory and config.redis_url:
        try:
            from langgraph.checkpoint.redis import RedisSaver

            ctx = RedisSaver.from_conn_string(config.redis_url)
            checkpointer = ctx.__enter__()
            checkpointer.setup()
            logger.info("%s 使用 Redis checkpointer（同步）", colorize("[memory]", "green"))
            return checkpointer
        except Exception as exc:
            logger.warning("%s 同步 RedisSaver 初始化失败，降级内存: %s",
                           colorize("[memory]", "yellow"), exc)
    if backend == "memory":
        logger.info("%s 使用内存 checkpointer", colorize("[memory]", "green"))
    return InMemorySaver()
