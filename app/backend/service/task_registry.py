"""TaskRegistry：thread_id → asyncio.Task 注册表。

P3 交付物：替代旧 workflow_service.py 的 _cancel_flags 机制。
- task.cancel() 使 CancelledError 在 generator 内部的 await 点抛出，
  真正中断 LangGraph 的 LLM 调用，不再依赖节点间隙轮询。
- Redis 兜底信号（多 worker 场景）：单机开发模式可延迟实现，接口先留。
- 同一 thread 并发 /run → ConcurrentRunError → router 层转 HTTP 409。

线程模型约定：
| 场景 | 行为 |
|------|------|
| 同一 thread 并发 /run | 409 Conflict（ConcurrentRunError） |
| 同一 thread /run 时 /resume | 409（同上） |
| 不同 thread 并发 | 正常并行（各自 generator + task） |
| 运行中 /cancel | task.cancel() → run.cancelled 事件 → 流关闭 |
| 未运行 /cancel | 200/202（幂等，不报错） |
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("backend.task_registry")


class ConcurrentRunError(Exception):
    """同一 thread 已有运行中的任务时抛出。router 层转 HTTP 409。"""

    def __init__(self, thread_id: str):
        self.thread_id = thread_id
        super().__init__(f"Thread {thread_id} already has a running task")


@dataclass
class RunningTask:
    """注册表中的运行中任务条目。"""

    thread_id: str
    run_id: str
    task: asyncio.Task
    started_at: float = field(default_factory=time.time)


class TaskRegistry:
    """thread_id → asyncio.Task 注册表；单进程权威 + Redis 兜底（多 worker）。

    用法：
        registry = TaskRegistry(redis=None)  # 单机模式
        task = await registry.register(thread_id, run_id, coro)
        success = await registry.cancel(thread_id)
    """

    def __init__(self, redis=None):
        self._tasks: dict[str, RunningTask] = {}
        self._redis = redis

    @property
    def redis(self):
        return self._redis

    @redis.setter
    def redis(self, value):
        self._redis = value

    async def register(
        self,
        thread_id: str,
        run_id: str,
        coro,
    ) -> asyncio.Task:
        """注册一个研究任务。

        Args:
            thread_id: 会话 ID
            run_id: 本次运行的唯一标识
            coro: 待执行的协程（通常是 async generator 的消费协程）

        Returns:
            创建的 asyncio.Task 实例

        Raises:
            ConcurrentRunError: 该 thread 已有运行中的任务
        """
        existing = self._tasks.get(thread_id)
        if existing is not None and not existing.task.done():
            raise ConcurrentRunError(thread_id)

        task = asyncio.create_task(coro, name=f"research:{thread_id}")
        self._tasks[thread_id] = RunningTask(
            thread_id=thread_id,
            run_id=run_id,
            task=task,
        )

        # Redis 兜底：标记该 thread 有运行中任务（多 worker 场景）
        if self._redis is not None:
            try:
                await self._redis.setex(f"cancel:{thread_id}", 86400, "running")
            except Exception as exc:
                logger.warning("Redis 标记 running 失败（降级为进程内）: %s", exc)

        # 任务完成后自动清理
        task.add_done_callback(lambda _: self._cleanup(thread_id))
        logger.info(
            "TaskRegistry 注册 | thread=%s | run=%s | total_active=%d",
            thread_id, run_id, len(self._tasks),
        )
        return task

    async def cancel(self, thread_id: str) -> bool:
        """取消运行中的任务。

        Returns:
            True: 本进程命中并已发送 cancel()
            False: 本进程无该任务（已结束或不在本 worker），仅 Redis 兜底信号
        """
        entry = self._tasks.get(thread_id)
        if entry is None or entry.task.done():
            # 本进程没有 → Redis 发兜底信号（多 worker 场景）
            if self._redis is not None:
                try:
                    await self._redis.set(f"cancel:{thread_id}", "1")
                except Exception as exc:
                    logger.warning("Redis 兜底信号发送失败: %s", exc)
            logger.info("TaskRegistry cancel 未命中本进程 | thread=%s", thread_id)
            return False

        entry.task.cancel()
        logger.info(
            "TaskRegistry cancel 命中 | thread=%s | run=%s",
            thread_id, entry.run_id,
        )
        return True

    def is_running(self, thread_id: str) -> bool:
        """检查某 thread 是否有运行中的任务。"""
        entry = self._tasks.get(thread_id)
        return entry is not None and not entry.task.done()

    def get_running(self, thread_id: str) -> Optional[RunningTask]:
        """获取运行中的任务条目（无则 None）。"""
        entry = self._tasks.get(thread_id)
        if entry is not None and not entry.task.done():
            return entry
        return None

    async def scan_orphans(self, graph_app=None) -> list[str]:
        """崩溃恢复扫描：进程重启后扫 PG checkpointer 标记中断会话。

        流程：
        1. 扫 Redis 中 cancel:{thread_id} 值为 "running" 但进程内无对应 task 的 thread
        2. 若有 graph_app，进一步检查 PG checkpointer 中该 thread 是否有 checkpoint
        3. 标记 Redis 写 thread:{thread_id}:interrupted_by_restart（TTL 7 天）

        Returns:
            被标记为 interrupted_by_restart 的 thread_id 列表
        """
        orphaned: list[str] = []

        if self._redis is None:
            # 单机模式无 Redis，跳过扫描（无多 worker 场景）
            logger.info("TaskRegistry scan_orphans 跳过（无 Redis）")
            return orphaned

        try:
            # 扫描所有 cancel: 前缀的 key
            keys = await self._redis.keys("cancel:*")
            for key in keys:
                if isinstance(key, bytes):
                    key = key.decode("utf-8")
                thread_id = key.replace("cancel:", "")
                value = await self._redis.get(key)

                # 值为 "running" 说明进程重启前没来得及清理
                if value != "running":
                    continue

                # 进程内已有 task 的跳过
                if self.is_running(thread_id):
                    continue

                # 双条件判定：PG checkpoint 存在且 next 非空
                has_checkpoint = False
                if graph_app is not None:
                    try:
                        config = {"configurable": {"thread_id": thread_id}}
                        snapshot = await graph_app.aget_state(config)
                        if snapshot and snapshot.next:
                            has_checkpoint = True
                    except Exception:
                        pass

                if not has_checkpoint:
                    # 无 checkpoint，清理 Redis 拘留标记
                    await self._redis.delete(key)
                    continue

                # 标记为 interrupted_by_restart
                await self._redis.setex(
                    f"thread:{thread_id}:interrupted_by_restart",
                    7 * 86400,  # 7 天 TTL
                    "1",
                )
                orphaned.append(thread_id)
                logger.info(
                    "TaskRegistry scan_orphans 标记 | thread=%s | interrupted_by_restart",
                    thread_id,
                )

        except Exception as exc:
            logger.warning("TaskRegistry scan_orphans 失败: %s", exc)

        return orphaned

    async def is_interrupted_by_restart(self, thread_id: str) -> bool:
        """检查某 thread 是否被重启中断标记过。"""
        if self._redis is None:
            return False
        try:
            value = await self._redis.get(f"thread:{thread_id}:interrupted_by_restart")
            if isinstance(value, bytes):
                value = value.decode("utf-8")
            return value == "1"
        except Exception:
            return False

    async def clear_interrupted_by_restart(self, thread_id: str) -> None:
        """清除重启中断标记（续跑成功后调用）。"""
        if self._redis is None:
            return
        try:
            await self._redis.delete(f"thread:{thread_id}:interrupted_by_restart")
        except Exception:
            pass

    def _cleanup(self, thread_id: str) -> None:
        """任务完成后的清理回调。"""
        entry = self._tasks.pop(thread_id, None)
        if entry is not None:
            # 清理 Redis 标记（异步，容错）
            if self._redis is not None:
                try:
                    loop = asyncio.get_event_loop()
                    loop.create_task(self._redis.delete(f"cancel:{thread_id}"))
                except Exception:
                    pass
            logger.info(
                "TaskRegistry 清理 | thread=%s | run=%s | remaining=%d",
                thread_id, entry.run_id, len(self._tasks),
            )


# ── 单例 ──────────────────────────────────────────

_REGISTRY: TaskRegistry | None = None


def get_task_registry() -> TaskRegistry:
    """获取全局 TaskRegistry 单例。"""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = TaskRegistry()
    return _REGISTRY


def init_task_registry(redis=None) -> TaskRegistry:
    """初始化全局 TaskRegistry（带 Redis 连接）。"""
    global _REGISTRY
    _REGISTRY = TaskRegistry(redis=redis)
    return _REGISTRY
