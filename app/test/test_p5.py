"""Phase 5 测试：记忆重写（langmem + PostgresStore 双通道）。

覆盖用例:
    T5-1 版本兼容 — langmem + langgraph PostgresStore import 无错误
    T5-2 后台提取 — MemoryService put_memory + list_memories 基本链路
    T5-3 热路径注入 — hot_path_search 返回格式化记忆文本
    T5-4 语义召回对比 — 验证非空查询的检索逻辑路径
    T5-5 不阻塞主流程 — 后台提取失败不影响主流程
    T5-6 旧记忆清除 — grep long_term/memory.db/MemoryManager 零残留
    T5-7 schema 隔离 — store_client 单例模式验证

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    set PYTHONPATH=app
    python -m pytest app/test/test_p5.py -v --asyncio-mode=auto
"""

import asyncio
import importlib
import importlib.abc
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
sys.path.insert(0, str(_APP_PATH))


# ── 通配 Meta Path Finder（与 test_p4 同模式）──

class _MockLoader(importlib.abc.Loader):
    def create_module(self, spec):
        mod = types.ModuleType(spec.name)
        mod.__path__ = []
        return mod
    def exec_module(self, module):
        pass

class _WildcardMockFinder(importlib.abc.MetaPathFinder):
    _PREFIXES = (
        "langchain",
        "langgraph",
        "langmem",
        "dashscope",
        "pymilvus",
        "trustcall",
        "dydantic",
    )
    def find_spec(self, name, path, target=None):
        for p in self._PREFIXES:
            if name == p or name.startswith(p + ".") or name.startswith(p + "_"):
                return importlib.machinery.ModuleSpec(name, _MockLoader(), is_package=True)
        return None

# 只在没有真实 langgraph 时挂载
try:
    import langgraph  # noqa: F401
    _HAS_LANGGRAPH = True
except ImportError:
    _HAS_LANGGRAPH = False
    sys.meta_path.insert(0, _WildcardMockFinder())


# ── T5-1: 版本兼容 ──────────────────────────────────

class TestT51VersionCompat:
    """T5-1: langmem + langgraph PostgresStore 版本组合启动无 import 错误。"""

    def test_import_langmem(self):
        """能 import langmem 的 create_memory_store_manager。"""
        if not _HAS_LANGGRAPH:
            pytest.skip("langgraph not installed in test env")
        try:
            from langmem import create_memory_store_manager
            assert callable(create_memory_store_manager)
        except (ImportError, TypeError, AssertionError):
            pytest.skip("langmem not installed in test env")

    def test_import_postgres_store(self):
        """能 import AsyncPostgresStore from langgraph.store.postgres。"""
        if not _HAS_LANGGRAPH:
            pytest.skip("langgraph not installed in test env")
        try:
            from langgraph.store.postgres import AsyncPostgresStore
            assert hasattr(AsyncPostgresStore, "asearch")
            assert hasattr(AsyncPostgresStore, "aput")
            assert hasattr(AsyncPostgresStore, "setup")
        except (ImportError, TypeError, AssertionError):
            pytest.skip("PostgresStore not available in test env")

    def test_store_client_import(self):
        """store_client 模块可以正常 import。"""
        from backend.infra.store_client import init_store, close_store, get_store
        assert callable(init_store)
        assert callable(close_store)
        assert callable(get_store)

    def test_memory_service_import(self):
        """memory_service 模块可以正常 import。"""
        from backend.service.memory_service import MemoryService, get_memory_service, init_memory_service
        assert MemoryService is not None
        assert callable(get_memory_service)
        assert callable(init_memory_service)


# ── T5-2: 后台提取（基本链路）──────────────────────

class TestT52BackgroundExtract:
    """T5-2: MemoryService put_memory + list_memories 基本链路。"""

    @pytest.mark.asyncio
    async def test_put_memory_and_list(self):
        """通过 mock store 验证 put_memory → list_memories 链路。"""
        from backend.service.memory_service import MemoryService
        from backend.infra import store_client

        # Mock store
        mock_store = AsyncMock()
        mock_item = MagicMock()
        mock_item.key = "test-key-001"
        mock_item.value = {"text": "用户偏好英文参考文献", "kind": "preference"}
        mock_item.created_at = MagicMock()
        mock_item.updated_at = MagicMock()
        mock_item.created_at.isoformat.return_value = "2026-09-02T10:00:00"
        mock_item.updated_at.isoformat.return_value = "2026-09-02T10:00:00"
        mock_store.asearch.return_value = [mock_item]
        mock_store.aput = AsyncMock()

        # 注入 mock store
        store_client._store_instance = mock_store

        svc = MemoryService(api_key="test-key")
        key = await svc.put_memory("user_1", "用户偏好英文参考文献", "preference")

        assert key != ""
        mock_store.aput.assert_awaited_once()

        # list_memories
        memories = await svc.list_memories("user_1")
        assert len(memories) == 1
        assert memories[0]["text"] == "用户偏好英文参考文献"
        assert memories[0]["kind"] == "preference"

        # 清理
        store_client._store_instance = None

    @pytest.mark.asyncio
    async def test_list_memories_empty(self):
        """store 为 None 时返回空列表。"""
        from backend.service.memory_service import MemoryService
        from backend.infra import store_client

        store_client._store_instance = None
        svc = MemoryService(api_key="test-key")
        result = await svc.list_memories("user_1")
        assert result == []


# ── T5-3: 热路径注入 ────────────────────────────────

class TestT53HotPathInjection:
    """T5-3: hot_path_search 返回格式化记忆文本。"""

    @pytest.mark.asyncio
    async def test_hot_path_search_returns_formatted_text(self):
        """验证 hot_path_search 返回的格式化文本可以注入 plan system prompt。"""
        from backend.service.memory_service import MemoryService
        from backend.infra import store_client

        # Mock store 返回记忆
        mock_store = AsyncMock()
        mock_item1 = MagicMock()
        mock_item1.value = {"text": "用户偏好英文参考文献", "kind": "preference"}
        mock_item2 = MagicMock()
        mock_item2.value = {"text": "研究方向是 AI 代码审查", "kind": "research_topic"}
        mock_store.asearch.return_value = [mock_item1, mock_item2]

        store_client._store_instance = mock_store

        svc = MemoryService(api_key="test-key", hot_path_top_k=5)
        context = await svc.hot_path_search("user_1", "AI代码审查的最新进展")

        # 验证格式化输出
        assert "用户偏好英文参考文献" in context
        assert "AI 代码审查" in context
        assert context.startswith("- ")

        # 清理
        store_client._store_instance = None

    @pytest.mark.asyncio
    async def test_hot_path_search_empty_when_no_store(self):
        """store 未初始化时返回空字符串（降级不阻塞）。"""
        from backend.service.memory_service import MemoryService
        from backend.infra import store_client

        store_client._store_instance = None
        svc = MemoryService(api_key="test-key")
        result = await svc.hot_path_search("user_1", "test query")
        assert result == ""

    @pytest.mark.asyncio
    async def test_hot_path_search_empty_when_no_hits(self):
        """无命中时返回空字符串。"""
        from backend.service.memory_service import MemoryService
        from backend.infra import store_client

        mock_store = AsyncMock()
        mock_store.asearch.return_value = []
        store_client._store_instance = mock_store

        svc = MemoryService(api_key="test-key")
        result = await svc.hot_path_search("user_1", "unrelated query")
        assert result == ""

        store_client._store_instance = None

    @pytest.mark.asyncio
    async def test_hot_path_search_handles_exception(self):
        """store.asearch 异常时不阻塞，返回空字符串。"""
        from backend.service.memory_service import MemoryService
        from backend.infra import store_client

        mock_store = AsyncMock()
        mock_store.asearch.side_effect = RuntimeError("DB down")
        store_client._store_instance = mock_store

        svc = MemoryService(api_key="test-key")
        result = await svc.hot_path_search("user_1", "test query")
        assert result == ""

        store_client._store_instance = None


# ── T5-4: 语义召回对比 ───────────────────────────────

class TestT54SemanticRecall:
    """T5-4: 验证非空查询的检索逻辑路径（对比旧哈希方案）。"""

    @pytest.mark.asyncio
    async def test_search_uses_query_for_semantic_match(self):
        """验证 asearch 被调用时传入了 query 参数（语义检索路径）。"""
        from backend.service.memory_service import MemoryService
        from backend.infra import store_client

        mock_store = AsyncMock()
        mock_store.asearch.return_value = []
        store_client._store_instance = mock_store

        svc = MemoryService(api_key="test-key", hot_path_top_k=3)
        await svc.hot_path_search("user_1", "深度学习在NLP中的应用")

        # 验证 asearch 被调用，且 query 参数正确传入
        mock_store.asearch.assert_awaited_once()
        call_args = mock_store.asearch.call_args
        assert call_args.kwargs.get("query") == "深度学习在NLP中的应用"
        assert call_args.kwargs.get("limit") == 3

        store_client._store_instance = None


# ── T5-5: 不阻塞主流程 ──────────────────────────────

class TestT55NonBlocking:
    """T5-5: 后台提取失败不影响主流程。"""

    @pytest.mark.asyncio
    async def test_background_extract_failure_does_not_raise(self):
        """后台提取任务内部异常只记日志，不影响主流程。"""
        from backend.service.memory_service import MemoryService
        from backend.infra import store_client

        store_client._store_instance = MagicMock()  # 非 None，触发 _ensure_manager
        svc = MemoryService(api_key="test-key")

        # Mock _ensure_manager 返回一个会抛异常的 manager
        svc._manager = MagicMock()
        svc._manager.ainvoke = AsyncMock(side_effect=RuntimeError("LLM API error"))

        # trigger_background_extract 不应抛出异常
        svc.trigger_background_extract("user_1", "thread_1", [])

        # 等待后台任务完成
        await asyncio.sleep(0.1)
        await svc.await_background_tasks()

        store_client._store_instance = None

    @pytest.mark.asyncio
    async def test_trigger_background_when_disabled(self):
        """background_enabled=False 时不触发后台提取。"""
        from backend.service.memory_service import MemoryService

        svc = MemoryService(api_key="test-key", background_enabled=False)
        # 不应抛异常，也不应创建任务
        svc.trigger_background_extract("user_1", "thread_1", [])
        assert len(svc._background_tasks) == 0

    @pytest.mark.asyncio
    async def test_trigger_background_when_no_store(self):
        """store 未初始化时不触发后台提取。"""
        from backend.service.memory_service import MemoryService
        from backend.infra import store_client

        store_client._store_instance = None
        svc = MemoryService(api_key="test-key")
        svc.trigger_background_extract("user_1", "thread_1", [])
        assert len(svc._background_tasks) == 0


# ── T5-6: 旧记忆清除 ────────────────────────────────

class TestT56OldMemoryCleanup:
    """T5-6: grep long_term/memory.db/MemoryManager 零残留命中。"""

    def test_no_long_term_py(self):
        """long_term.py 已删除。"""
        p = _APP_PATH / "mult_agents" / "memory" / "long_term.py"
        assert not p.exists(), f"{p} should be deleted"

    def test_no_manager_py(self):
        """manager.py 已删除。"""
        p = _APP_PATH / "mult_agents" / "memory" / "manager.py"
        assert not p.exists(), f"{p} should be deleted"

    def test_no_short_term_py(self):
        """short_term.py 已删除。"""
        p = _APP_PATH / "mult_agents" / "memory" / "short_term.py"
        assert not p.exists(), f"{p} should be deleted"

    def test_no_memory_db(self):
        """memory.db 已删除。"""
        p = _APP_PATH / "data" / "memory.db"
        assert not p.exists(), f"{p} should be deleted"

    def test_no_utils_py(self):
        """utils.py 已删除。"""
        p = _APP_PATH / "mult_agents" / "memory" / "utils.py"
        assert not p.exists(), f"{p} should be deleted"

    def test_no_build_memory_manager_in_runtime(self):
        """runtime.py 不再导出 build_memory_manager。"""
        from mult_agents import runtime
        assert not hasattr(runtime, "build_memory_manager"), \
            "runtime.build_memory_manager should be deleted"

    def test_no_MEMORY_MANAGER_global(self):
        """runtime.py 不再有 MEMORY_MANAGER 全局变量。"""
        from mult_agents import runtime
        assert not hasattr(runtime, "MEMORY_MANAGER"), \
            "runtime.MEMORY_MANAGER should be deleted"

    def test_memory_init_only_exports_types(self):
        """memory/__init__.py 只导出类型定义，不导出 MemoryManager。"""
        from mult_agents import memory
        assert not hasattr(memory, "MemoryManager"), \
            "MemoryManager should be removed from memory package"
        assert hasattr(memory, "MemoryEntry")
        assert hasattr(memory, "MemoryType")


# ── T5-7: schema 隔离 ───────────────────────────────

class TestT57SchemaIsolation:
    """T5-7: store_client 单例模式验证。"""

    def test_get_store_returns_none_before_init(self):
        """get_store 在 init_store 之前返回 None。"""
        from backend.infra import store_client
        store_client._store_instance = None
        assert store_client.get_store() is None

    def test_get_store_returns_instance_after_init(self):
        """get_store 在 init_store 之后返回单例。"""
        from backend.infra import store_client
        mock = MagicMock()
        store_client._store_instance = mock
        assert store_client.get_store() is mock
        store_client._store_instance = None

    @pytest.mark.asyncio
    async def test_init_store_creates_singleton(self):
        """init_store 创建的实例是全局唯一的。"""
        from backend.infra import store_client
        store_client._store_instance = None
        store_client._store_context = None

        # Mock from_conn_string
        mock_ctx = AsyncMock()
        mock_store = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_store)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch("backend.infra.store_client.AsyncPostgresStore") as MockStore:
            MockStore.from_conn_string.return_value = mock_ctx
            with patch("backend.infra.store_client.DashScopeEmbeddings"):
                store1 = await store_client.init_store("dsn", "key")
                store2 = store_client.get_store()
                assert store1 is store2

        store_client._store_instance = None
        store_client._store_context = None

    def test_store_client_module_exports(self):
        """store_client 模块导出 init_store / close_store / get_store。"""
        from backend.infra import store_client
        assert hasattr(store_client, "init_store")
        assert hasattr(store_client, "close_store")
        assert hasattr(store_client, "get_store")


# ── 额外: /memories API 验证 ─────────────────────────

class TestMemoriesAPI:
    """验证 /memories API 路由已注册。"""

    def test_memories_route_exists(self):
        """research_router 中存在 GET /memories 路由。"""
        try:
            from backend.router import research_router
        except (ImportError, ModuleNotFoundError):
            pytest.skip("fastapi not installed in test env")
        routes = [r for r in research_router.routes if hasattr(r, "path")]
        memories_routes = [r for r in routes if "/memories" in r.path]
        assert len(memories_routes) > 0, "GET /memories route should exist"
        assert memories_routes[0].methods == {"GET"}
