"""Phase 3 测试：TaskRegistry、并发拦截、cancel 幂等、resume 语义、状态 API。

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    set PYTHONPATH=app
    python -m pytest app/test/test_p3.py -v --asyncio-mode=auto

注意：直接从模块文件导入，避免经过 backend.service.__init__.py 的 langgraph 依赖链。
"""

import asyncio
import collections.abc
import importlib.util
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
sys.path.insert(0, str(_APP_PATH))

# 直接从模块文件导入，绕过 backend/service/__init__.py 的 langgraph 依赖链
_spec_tr = importlib.util.spec_from_file_location(
    "backend.service.task_registry",
    _APP_PATH / "backend" / "service" / "task_registry.py",
)
_mod_tr = importlib.util.module_from_spec(_spec_tr)
spec_tr_loaded = False
try:
    _spec_tr.loader.exec_module(_mod_tr)
    spec_tr_loaded = True
except ImportError:
    pass

if spec_tr_loaded:
    TaskRegistry = _mod_tr.TaskRegistry
    ConcurrentRunError = _mod_tr.ConcurrentRunError
    RunningTask = _mod_tr.RunningTask
    get_task_registry = _mod_tr.get_task_registry
    init_task_registry = _mod_tr.init_task_registry
else:
    TaskRegistry = None
    ConcurrentRunError = Exception


# ──────────────────────────────────────────────
# T3-1 TaskRegistry 基本功能
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_registry_register_and_cleanup():
    """注册任务后能在注册表中找到，完成后自动清理。"""
    registry = TaskRegistry(redis=None)

    async def dummy_coro():
        await asyncio.sleep(0.1)

    task = await registry.register("t1", "r1", dummy_coro())
    assert registry.is_running("t1")
    entry = registry.get_running("t1")
    assert entry is not None
    assert entry.run_id == "r1"

    await task  # 等待完成
    # done_callback 应已清理
    assert not registry.is_running("t1")


@pytest.mark.asyncio
async def test_task_registry_concurrent_run_raises_409():
    """同一 thread 并发 /run → ConcurrentRunError。"""
    registry = TaskRegistry(redis=None)

    async def long_coro():
        await asyncio.sleep(10)

    await registry.register("t1", "r1", long_coro())

    with pytest.raises(ConcurrentRunError) as exc_info:
        await registry.register("t1", "r2", long_coro())
    assert exc_info.value.thread_id == "t1"

    # 清理
    await registry.cancel("t1")
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_task_registry_cancel():
    """cancel() 命中本进程 → True，CancelledError 传播。"""
    registry = TaskRegistry(redis=None)

    cancelled_was_raised = False

    async def cancellable_coro():
        nonlocal cancelled_was_raised
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled_was_raised = True
            raise

    task = await registry.register("t1", "r1", cancellable_coro())
    assert registry.is_running("t1")

    # 让 task 先开始执行
    await asyncio.sleep(0.05)

    hit = await registry.cancel("t1")
    assert hit is True

    # 等待 CancelledError 传播并完成
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert cancelled_was_raised
    assert task.cancelled()
    assert not registry.is_running("t1")


@pytest.mark.asyncio
async def test_task_registry_cancel_not_running_idempotent():
    """未运行的 thread /cancel → False（幂等，不报错）。"""
    registry = TaskRegistry(redis=None)

    hit = await registry.cancel("nonexistent")
    assert hit is False
    # 不抛异常即为幂等验证通过


@pytest.mark.asyncio
async def test_task_registry_cancel_done_task():
    """已完成的 task /cancel → False（幂等）。"""
    registry = TaskRegistry(redis=None)

    async def quick_coro():
        pass

    task = await registry.register("t1", "r1", quick_coro())
    await task
    await asyncio.sleep(0.05)  # 等 done_callback

    hit = await registry.cancel("t1")
    assert hit is False


# ──────────────────────────────────────────────
# T3-2 并发拦截（ConcurrentRunError 类型验证）
# ──────────────────────────────────────────────


def test_concurrent_run_error_is_exception():
    """ConcurrentRunError 是 Exception 子类，可被 FastAPI 捕获。"""
    assert issubclass(ConcurrentRunError, Exception)
    err = ConcurrentRunError("t1")
    assert err.thread_id == "t1"
    assert "t1" in str(err)


# ──────────────────────────────────────────────
# T3-6 cancel 幂等（连续两次 cancel 不报错）
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_idempotent_double_cancel():
    """连续两次 /cancel → 均不报错。"""
    registry = TaskRegistry(redis=None)

    async def quick_coro():
        await asyncio.sleep(0.05)

    task = await registry.register("t1", "r1", quick_coro())
    await task
    await asyncio.sleep(0.05)

    # 第一次 cancel（任务已完成）
    hit1 = await registry.cancel("t1")
    assert hit1 is False

    # 第二次 cancel（仍幂等）
    hit2 = await registry.cancel("t1")
    assert hit2 is False


# ──────────────────────────────────────────────
# T3-3 resume 语义验证（mode=continue 用 None 输入）
# ──────────────────────────────────────────────


def test_resume_request_mode_validation():
    """ResumeRequest 的 mode 字段只接受 continue / answer。"""
    # 延迟导入，避免 langgraph 依赖
    from backend.schemas.research import ResumeRequest

    # mode=continue 合法
    req = ResumeRequest(thread_id="t1", mode="continue")
    assert req.mode == "continue"
    assert req.resume_value is None

    # mode=answer 合法（带 resume_value）
    req2 = ResumeRequest(thread_id="t1", mode="answer", resume_value={"action": "approve"})
    assert req2.mode == "answer"

    # 非法 mode → ValidationError
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ResumeRequest(thread_id="t1", mode="invalid")


# ──────────────────────────────────────────────
# T3-7 状态 API 验证
# ──────────────────────────────────────────────


def test_running_task_dataclass():
    """RunningTask 数据类可正确构造。"""
    import asyncio

    async def coro():
        pass

    # Python 3.14 需要显式创建事件循环
    loop = asyncio.new_event_loop()
    task = loop.create_task(coro())
    rt = RunningTask(thread_id="t1", run_id="r1", task=task)
    assert rt.thread_id == "t1"
    assert rt.run_id == "r1"
    assert rt.task is task
    assert isinstance(rt.started_at, float)

    # 清理
    task.cancel()
    loop.close()


# ──────────────────────────────────────────────
# T3-4 崩溃恢复扫描（无 Redis 时跳过）
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scan_orphans_no_redis_skips():
    """无 Redis 时 scan_orphans 跳过，返回空列表。"""
    registry = TaskRegistry(redis=None)
    orphaned = await registry.scan_orphans(graph_app=None)
    assert orphaned == []


# ──────────────────────────────────────────────
# TaskRegistry 单例测试
# ──────────────────────────────────────────────


def test_get_task_registry_singleton():
    """get_task_registry 返回同一实例。"""
    # 重置全局实例
    _mod_tr._REGISTRY = None

    r1 = get_task_registry()
    r2 = get_task_registry()
    assert r1 is r2

    # 清理
    _mod_tr._REGISTRY = None


def test_init_task_registry_with_redis():
    """init_task_registry 创建带 Redis 的新实例。"""
    _mod_tr._REGISTRY = None

    mock_redis = MagicMock()
    registry = init_task_registry(redis=mock_redis)
    assert registry.redis is mock_redis

    # 清理
    _mod_tr._REGISTRY = None


# ──────────────────────────────────────────────
# TaskRegistry 与多线程安全验证
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_different_threads_run_independently():
    """不同 thread 并发运行互不干扰。"""
    registry = TaskRegistry(redis=None)

    async def coro_t1():
        await asyncio.sleep(0.1)

    async def coro_t2():
        await asyncio.sleep(0.1)

    task1 = await registry.register("t1", "r1", coro_t1())
    task2 = await registry.register("t2", "r2", coro_t2())

    assert registry.is_running("t1")
    assert registry.is_running("t2")
    assert registry.get_running("t1").run_id == "r1"
    assert registry.get_running("t2").run_id == "r2"

    await task1
    await task2
    await asyncio.sleep(0.05)

    assert not registry.is_running("t1")
    assert not registry.is_running("t2")
