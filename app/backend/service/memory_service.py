"""P5-2: 记忆服务层 — langmem 双通道（后台提取 + 热路径检索）。

架构：
  1. 后台提取（run.completed 后异步触发）：
     - 用 langmem create_memory_store_manager 从对话中提取用户偏好/研究主题
     - 写入 PostgresStore，namespace=(user_id, "memories")
     - 不阻塞主流程（asyncio.create_task）
  2. 热路径检索（plan 节点前注入）：
     - store.asearch(namespace, query=用户问题, limit=5)
     - 注入 state["memory_context"]，供 plan system prompt 使用

参考: memory-template:src/chatbot/graph.py:31-36
       memory-template:src/memory_graph/graph.py:48-52
"""

import asyncio
import logging
import uuid
from typing import Optional

from langchain_community.chat_models import ChatTongyi
from langchain_core.messages import HumanMessage
from langmem import create_memory_store_manager

from backend.infra.store_client import get_store

logger = logging.getLogger("backend.memory_service")


class MemoryService:
    """记忆服务：langmem 双通道（后台提取 + 热路径检索）。

    生命周期：
    - init(): 构建 langmem manager（延迟到首次使用）
    - hot_path_search(): plan 节点前同步调用
    - background_extract(): run.completed 后异步触发
    """

    def __init__(
        self,
        api_key: str,
        model: str = "qwen-turbo",
        hot_path_top_k: int = 5,
        background_enabled: bool = True,
    ):
        self._api_key = api_key
        self._model = model
        self._hot_path_top_k = hot_path_top_k
        self._background_enabled = background_enabled
        self._manager = None
        # 后台任务集合（防止 GC 回收 + shutdown 时 await）
        self._background_tasks: set[asyncio.Task] = set()

    def _ensure_manager(self):
        """延迟构建 langmem create_memory_store_manager。"""
        if self._manager is not None:
            return self._manager

        store = get_store()
        if store is None:
            logger.warning("PostgresStore 未初始化，langmem manager 构建跳过")
            return None

        # 提取用 LLM（用 qwen-turbo 省成本）
        llm = ChatTongyi(model=self._model, temperature=0.1, dashscope_api_key=self._api_key)

        self._manager = create_memory_store_manager(
            llm,
            namespace=("memories", "{user_id}"),
            store=store,
            enable_inserts=True,
        )
        logger.info("langmem memory_store_manager 构建完成 | model=%s", self._model)
        return self._manager

    # ── 热路径检索（plan 前） ─────────────────────────────

    async def hot_path_search(self, user_id: str, query: str) -> str:
        """从 PostgresStore 检索记忆，格式化为 plan system prompt 注入文本。

        Args:
            user_id: 用户 ID
            query: 用户查询文本

        Returns:
            格式化的记忆上下文字符串（无记忆时返回空字符串）
        """
        store = get_store()
        if store is None:
            logger.warning("[memory] hot_path_search 跳过: PostgresStore 未初始化")
            return ""

        namespace = (user_id, "memories")
        try:
            items = await store.asearch(
                namespace, query=query, limit=self._hot_path_top_k
            )
        except Exception as exc:
            logger.warning("[memory] hot_path_search 异常: %s", exc)
            return ""

        if not items:
            logger.info(
                "[memory] hot_path_search 无命中 | user=%s | query=%s",
                user_id,
                query[:80],
            )
            return ""

        lines = []
        for item in items:
            text = item.value.get("text", "") if isinstance(item.value, dict) else str(item.value)
            if text:
                lines.append(f"- {text}")

        context = "\n".join(lines) if lines else ""

        logger.info(
            "[memory] hot_path_search 命中 | user=%s | count=%d | injected_chars=%d | query=%s",
            user_id,
            len(items),
            len(context),
            query[:80],
        )
        return context

    # ── 后台提取（run.completed 后） ─────────────────────

    def trigger_background_extract(
        self,
        user_id: str,
        thread_id: str,
        messages: list,
    ) -> None:
        """触发后台记忆提取（非阻塞，fire-and-forget）。

        在 run.completed 事件发出后调用。使用 asyncio.create_task 启动后台任务，
        任务引用存入 _background_tasks 集合防止 GC 回收。

        Args:
            user_id: 用户 ID
            thread_id: 会话 ID
            messages: LangChain 消息列表（HumanMessage + AIMessage）
        """
        if not self._background_enabled:
            return

        store = get_store()
        if store is None:
            return

        task = asyncio.create_task(
            self._do_background_extract(user_id, thread_id, messages)
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _do_background_extract(
        self,
        user_id: str,
        thread_id: str,
        messages: list,
    ) -> None:
        """实际执行后台记忆提取。

        错误处理：任何异常只记日志，不影响主流程（run.completed 已发出）。
        """
        manager = self._ensure_manager()
        if manager is None:
            return

        try:
            # 构造 config，传入 user_id 供 namespace 模板替换
            config = {"configurable": {"user_id": user_id}}

            # langmem manager.ainvoke 接收 messages + config
            await manager.ainvoke(
                {"messages": messages},
                config=config,
            )

            logger.info(
                "[memory] background_extract 完成 | user=%s | thread=%s",
                user_id,
                thread_id,
            )
        except Exception as exc:
            logger.warning(
                "[memory] background_extract 失败（不影响主流程） | user=%s | thread=%s | error=%s",
                user_id,
                thread_id,
                exc,
            )

    async def await_background_tasks(self) -> None:
        """等待所有后台任务完成（lifespan shutdown 时调用）。"""
        if not self._background_tasks:
            return
        logger.info("[memory] 等待 %d 个后台提取任务完成...", len(self._background_tasks))
        await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()

    # ── 直接写入（测试/手动入口） ─────────────────────────

    async def put_memory(
        self,
        user_id: str,
        text: str,
        kind: str = "general",
    ) -> str:
        """直接写入一条记忆到 PostgresStore。

        供测试和手动记忆写入使用。
        """
        store = get_store()
        if store is None:
            return ""

        key = uuid.uuid4().hex
        namespace = (user_id, "memories")
        await store.aput(
            namespace,
            key,
            {"text": text, "kind": kind},
            index=["text"],
        )
        logger.info("[memory] put_memory | user=%s | key=%s | kind=%s", user_id, key, kind)
        return key

    # ── 查询（供 /memories API） ─────────────────────────

    async def list_memories(
        self,
        user_id: str,
        query: str = "",
        limit: int = 200,
    ) -> list[dict]:
        """列出用户全部记忆条目（供 GET /memories API）。

        Args:
            user_id: 用户 ID
            query: 可选的语义查询文本（为空时返回全部）
            limit: 返回上限

        Returns:
            记忆条目列表 [{id, text, kind, created_at, updated_at}]
        """
        store = get_store()
        if store is None:
            return []

        namespace = (user_id, "memories")
        try:
            items = await store.asearch(
                namespace,
                query=query or None,
                limit=limit,
            )
        except Exception as exc:
            logger.warning("[memory] list_memories 异常: %s", exc)
            return []

        result = []
        for item in items:
            value = item.value if isinstance(item.value, dict) else {"text": str(item.value)}
            result.append({
                "id": item.key,
                "text": value.get("text", ""),
                "kind": value.get("kind", "general"),
                "created_at": item.created_at.isoformat() if hasattr(item, "created_at") and item.created_at else "",
                "updated_at": item.updated_at.isoformat() if hasattr(item, "updated_at") and item.updated_at else "",
            })
        return result


# ── 单例 ──────────────────────────────────────────

_SERVICE: Optional[MemoryService] = None


def get_memory_service() -> Optional[MemoryService]:
    """获取 MemoryService 单例（未初始化时返回 None）。"""
    return _SERVICE


def init_memory_service(
    api_key: str,
    model: str = "qwen-turbo",
    hot_path_top_k: int = 5,
    background_enabled: bool = True,
) -> MemoryService:
    """初始化 MemoryService 单例（lifespan 启动时调用）。"""
    global _SERVICE
    _SERVICE = MemoryService(
        api_key=api_key,
        model=model,
        hot_path_top_k=hot_path_top_k,
        background_enabled=background_enabled,
    )
    return _SERVICE
