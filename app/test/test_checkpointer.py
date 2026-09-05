"""P2-2 checkpointer 契约测试：异步 checkpointer 单例 + 同步降级工厂。

覆盖两条不变式：
1. 生产 async 执行链（astream/ainvoke）必须用 AsyncPostgresSaver（具备
   aget_tuple/aput/aput_writes），不能用 sync PostgresSaver（否则 graph.astream
   内部调 aget_tuple 落到基类 stub 抛 NotImplementedError）。
2. 连接失败时 init_checkpointer 降级 InMemorySaver 且不抛异常。
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def reset_checkpointer_singleton():
    """重置 runtime 模块的 checkpointer 全局单例，保证测试隔离。"""
    import mult_agents.runtime as rt

    rt._checkpointer_instance = None
    rt._checkpointer_context = None
    yield
    rt._checkpointer_instance = None
    rt._checkpointer_context = None


def _fake_config(**overrides):
    """构造最小 AppConfig 兼容对象。"""
    defaults = dict(
        checkpointer_backend="postgres",
        enable_memory=True,
        postgres_dsn="postgresql://root:pw@localhost:5432/mydb",
        redis_url="redis://:pw@localhost:6379",
    )
    defaults.update(overrides)
    return type("_Cfg", (), defaults)()


def test_async_postgres_saver_has_async_methods():
    """AsyncPostgresSaver 必须具备 aget_tuple/aput/aput_writes（async 能力不变式）。"""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    for method in ("aget_tuple", "aput", "aput_writes"):
        assert method in AsyncPostgresSaver.__dict__, (
            f"AsyncPostgresSaver.{method} 缺失，graph.astream 会调用基类 stub 抛 NotImplementedError"
        )


def test_sync_postgres_saver_lacks_async_methods():
    """sync PostgresSaver 无 async 方法，证明不能用它承接 astream（对比证据）。"""
    from langgraph.checkpoint.postgres import PostgresSaver

    for method in ("aget_tuple", "aput", "aput_writes"):
        assert method not in PostgresSaver.__dict__, (
            f"sync PostgresSaver 不应有 {method}；若出现说明 langgraph 版本行为变化，需重审 P2-2"
        )


@pytest.mark.asyncio
async def test_init_checkpointer_uses_async_saver(reset_checkpointer_singleton):
    """postgres backend + 有效 DSN → init_checkpointer 返回 AsyncPostgresSaver 实例。"""
    import mult_agents.runtime as rt
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.checkpoint.memory import InMemorySaver

    fake_saver = MagicMock(spec=AsyncPostgresSaver)
    fake_saver.setup = AsyncMock(return_value=None)

    class _FakeCtx:
        async def __aenter__(self):
            return fake_saver

        async def __aexit__(self, *exc):
            return False

    with patch.object(AsyncPostgresSaver, "from_conn_string", return_value=_FakeCtx()):
        result = await rt.init_checkpointer(_fake_config())

    assert result is fake_saver, "应返回 AsyncPostgresSaver 实例"
    assert not isinstance(result, InMemorySaver), "postgres 可用时不得降级内存"
    fake_saver.setup.assert_awaited_once()


@pytest.mark.asyncio
async def test_init_checkpointer_falls_back_to_memory_on_error(reset_checkpointer_singleton):
    """连接失败 → 降级 InMemorySaver 且不抛异常。"""
    import mult_agents.runtime as rt
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.checkpoint.memory import InMemorySaver

    with patch.object(
        AsyncPostgresSaver,
        "from_conn_string",
        side_effect=RuntimeError("connection refused"),
    ):
        result = await rt.init_checkpointer(_fake_config())

    assert isinstance(result, InMemorySaver), "连接失败必须降级内存 checkpointer"


@pytest.mark.asyncio
async def test_get_checkpointer_returns_singleton(reset_checkpointer_singleton):
    """init 后 get_checkpointer 返回同一实例；未 init 返回 None。"""
    import mult_agents.runtime as rt
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    assert rt.get_checkpointer() is None, "未初始化时应返回 None"

    fake_saver = MagicMock(spec=AsyncPostgresSaver)
    fake_saver.setup = AsyncMock(return_value=None)

    class _FakeCtx:
        async def __aenter__(self):
            return fake_saver

        async def __aexit__(self, *exc):
            return False

    with patch.object(AsyncPostgresSaver, "from_conn_string", return_value=_FakeCtx()):
        await rt.init_checkpointer(_fake_config())

    assert rt.get_checkpointer() is fake_saver, "init 后应返回同一单例"


def test_build_checkpointer_sync_uses_sync_saver(reset_checkpointer_singleton):
    """sync 场景（graph.invoke）→ build_checkpointer 选 sync PostgresSaver。"""
    import mult_agents.runtime as rt
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.checkpoint.memory import InMemorySaver

    fake_saver = MagicMock(spec=PostgresSaver)
    fake_saver.setup = MagicMock(return_value=None)

    class _FakeCtx:
        def __enter__(self):
            return fake_saver

        def __exit__(self, *exc):
            return False

    with patch.object(PostgresSaver, "from_conn_string", return_value=_FakeCtx()):
        result = rt.build_checkpointer(_fake_config())

    assert result is fake_saver, "sync 场景应返回 sync PostgresSaver"
    assert not isinstance(result, InMemorySaver)
    fake_saver.setup.assert_called_once()
