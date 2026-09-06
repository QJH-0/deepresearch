"""R3.2 多实例任务取消广播 — 单元测试。

覆盖: 本地命中直接取消、本地未命中广播、订阅方取消本地任务、
      已完成任务无副作用、畸形消息忽略、Redis 不可用降级、
      无 Redis 纯本地语义、订阅断线重连。
"""

import asyncio
import json
from unittest.mock import MagicMock, AsyncMock, patch

import pytest


def _make_registry(redis=None):
    from backend.service.task_registry import TaskRegistry
    return TaskRegistry(redis=redis)


def _make_mock_task(done=False, cancelled=False):
    """创建一个模拟的 asyncio.Task。"""
    task = MagicMock()
    task.done.return_value = done
    task.cancelled.return_value = cancelled
    return task


# ── T3.2-01 本地命中直接取消 ───────────────────────────────────────

class TestLocalCancelHit:
    def test_local_hit_cancels_no_broadcast(self):
        registry = _make_registry(redis=None)
        task = _make_mock_task(done=False)
        registry._tasks["t1"] = MagicMock(thread_id="t1", run_id="r1", task=task)

        result = asyncio.run(registry.cancel("t1"))

        assert result is True
        task.cancel.assert_called_once()


# ── T3.2-02 本地未命中广播 ─────────────────────────────────────────

class TestLocalMissBroadcast:
    def test_local_miss_publishes_and_setex(self):
        mock_redis = AsyncMock()
        registry = _make_registry(redis=mock_redis)

        result = asyncio.run(registry.cancel("t_missing"))

        assert result is True
        mock_redis.publish.assert_called_once()
        call_args = mock_redis.publish.call_args
        assert call_args.args[0] == "task:cancel"
        payload = json.loads(call_args.args[1])
        assert payload["thread_id"] == "t_missing"
        assert "instance_id" in payload
        assert "ts" in payload
        mock_redis.setex.assert_called_once()
        setex_args = mock_redis.setex.call_args
        assert setex_args.args[0] == "cancel:t_missing"
        assert setex_args.args[1] == 300


# ── T3.2-03 订阅方收到广播取消本地任务 ─────────────────────────────

class TestBroadcastHandling:
    def test_handle_cancel_broadcast_cancels_local(self):
        registry = _make_registry(redis=None)
        task = _make_mock_task(done=False)
        registry._tasks["t_remote"] = MagicMock(
            thread_id="t_remote", run_id="r1", task=task
        )

        raw = json.dumps({
            "thread_id": "t_remote",
            "instance_id": "other-host",
            "ts": 1725500000,
        }).encode("utf-8")

        asyncio.run(registry._handle_cancel_broadcast(raw))

        task.cancel.assert_called_once()


# ── T3.2-04 已完成任务无副作用 ────────────────────────────────────

class TestCompletedTaskNoop:
    def test_done_task_not_cancelled_by_broadcast(self):
        registry = _make_registry(redis=None)
        task = _make_mock_task(done=True)
        registry._tasks["t_done"] = MagicMock(
            thread_id="t_done", run_id="r1", task=task
        )

        raw = json.dumps({
            "thread_id": "t_done", "instance_id": "x", "ts": 0,
        }).encode("utf-8")

        asyncio.run(registry._handle_cancel_broadcast(raw))

        task.cancel.assert_not_called()


# ── T3.2-05 畸形广播消息被忽略 ─────────────────────────────────────

class TestMalformedMessage:
    def test_invalid_json_ignored(self):
        registry = _make_registry(redis=None)

        asyncio.run(registry._handle_cancel_broadcast(b"not json"))

    def test_missing_thread_id_ignored(self):
        registry = _make_registry(redis=None)
        task = _make_mock_task(done=False)
        registry._tasks["t1"] = MagicMock(thread_id="t1", run_id="r1", task=task)

        raw = json.dumps({"instance_id": "x", "ts": 0}).encode("utf-8")
        asyncio.run(registry._handle_cancel_broadcast(raw))

        task.cancel.assert_not_called()


# ── T3.2-06 Redis 不可用降级 ──────────────────────────────────────

class TestRedisUnavailable:
    def test_redis_publish_failure_returns_false(self):
        mock_redis = AsyncMock()
        mock_redis.publish.side_effect = ConnectionError("Redis down")
        registry = _make_registry(redis=mock_redis)

        result = asyncio.run(registry.cancel("t_fail"))

        assert result is False


# ── T3.2-07 无 Redis 时纯本地语义 ──────────────────────────────────

class TestNoRedisLocalOnly:
    def test_no_redis_returns_false_on_miss(self):
        registry = _make_registry(redis=None)

        result = asyncio.run(registry.cancel("nonexistent"))

        assert result is False


# ── T3.2-08 订阅断线重连 ───────────────────────────────────────────

class TestSubscriberReconnect:
    @pytest.mark.asyncio
    async def test_subscribe_reconnects_with_backoff(self):
        """验证订阅断线后重连：subscribe 第一次失败、第二次成功后 listen 阻塞。"""
        mock_redis = MagicMock()
        mock_pubsub = MagicMock()
        mock_redis.pubsub.return_value = mock_pubsub

        subscribe_count = 0

        async def mock_subscribe(*args, **kwargs):
            nonlocal subscribe_count
            subscribe_count += 1
            if subscribe_count == 1:
                raise ConnectionError("Connection lost")

        mock_pubsub.subscribe = mock_subscribe

        async def mock_listen():
            yield {"type": "subscribe", "data": 1}
            # 阻塞，模拟等待消息
            await asyncio.Event().wait()

        mock_pubsub.listen = mock_listen

        registry = _make_registry(redis=mock_redis)

        real_sleep = asyncio.sleep

        async def instant_sleep(seconds):
            pass

        asyncio.sleep = instant_sleep
        try:
            task = asyncio.create_task(registry._subscribe_loop())
            for _ in range(50):
                await real_sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            asyncio.sleep = real_sleep

        # subscribe 至少被调用 2 次，证明断线后重连
        assert subscribe_count >= 2
