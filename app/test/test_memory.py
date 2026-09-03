"""Phase 5/8 测试：记忆重写——提取/注入/召回/不阻塞（体系化收口）。

覆盖用例:
    T5-1 版本兼容 — 模块文件存在
    T5-2 后台提取 — MemoryService put_memory + list_memories 基本链路
    T5-3 热路径注入 — hot_path_search 返回格式化记忆文本
    T5-4 语义召回对比 — 验证非空查询的检索逻辑路径
    T5-5 不阻塞主流程 — 后台提取失败不影响主流程
    T5-6 旧记忆清除 — grep long_term/memory.db/MemoryManager 零残留
    T5-7 schema 隔离 — store_client 单例模式验证

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    python -m pytest app/test/test_memory.py -v --asyncio-mode=auto

注意：T5-2~T5-5 的测试需要 langmem 等依赖，在 CI 环境中通过
      test_p5.py 的 _WildcardMockFinder 实现 mock。本文件聚焦
      旧记忆清除（T5-6）和模块文件验证（T5-1），这些无需依赖。
      完整的 langmem 链路测试由 test_p5.py 覆盖。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
sys.path.insert(0, str(_APP_PATH))


def _can_import_memory_service():
    """尝试导入 memory_service，成功返回 True。"""
    try:
        from fastapi import APIRouter  # noqa: F401
        if not callable(APIRouter):
            return False
        # memory_service 可导入（conftest mock 了下级依赖）
        from backend.service.memory_service import MemoryService  # noqa: F401
        return True
    except (ImportError, TypeError, ModuleNotFoundError):
        return False


_HAS_MEMORY_SERVICE = _can_import_memory_service()


# ── T5-1: 模块文件验证 ─────────────────────────────


class TestModuleExists:
    """T5-1: 关键模块文件存在。"""

    def test_store_client_file_exists(self):
        assert (_APP_PATH / "backend" / "infra" / "store_client.py").exists()

    def test_memory_service_file_exists(self):
        assert (_APP_PATH / "backend" / "service" / "memory_service.py").exists()

    def test_memory_init_only_exports_types(self):
        """memory/__init__.py 只导出类型定义，不导出 MemoryManager。"""
        from mult_agents import memory
        assert not hasattr(memory, "MemoryManager"), "MemoryManager should be removed"
        assert hasattr(memory, "MemoryEntry")
        assert hasattr(memory, "MemoryType")


# ── T5-6: 旧记忆清除 ────────────────────────────────


class TestOldMemoryCleanup:
    """T5-6: 旧记忆文件零残留。"""

    def test_no_long_term_py(self):
        assert not (_APP_PATH / "mult_agents" / "memory" / "long_term.py").exists()

    def test_no_manager_py(self):
        assert not (_APP_PATH / "mult_agents" / "memory" / "manager.py").exists()

    def test_no_short_term_py(self):
        assert not (_APP_PATH / "mult_agents" / "memory" / "short_term.py").exists()

    def test_no_memory_db(self):
        assert not (_APP_PATH / "data" / "memory.db").exists()

    def test_no_utils_py(self):
        assert not (_APP_PATH / "mult_agents" / "memory" / "utils.py").exists()

    def test_no_build_memory_manager_in_runtime(self):
        from mult_agents import runtime
        assert not hasattr(runtime, "build_memory_manager")

    def test_no_MEMORY_MANAGER_global(self):
        from mult_agents import runtime
        assert not hasattr(runtime, "MEMORY_MANAGER")


# ── T5-2~T5-5: 记忆链路（依赖 langmem，由 test_p5.py 覆盖）──


class TestMemoryServiceLink:
    """T5-2~T5-5: MemoryService 链路测试。

    这些测试需要完整的 langmem + PostgresStore 依赖，
    由 test_p5.py 的 _WildcardMockFinder 提供 mock 环境。
    在没有依赖的环境中跳过。
    """

    @pytest.mark.skipif(not _HAS_MEMORY_SERVICE, reason="memory_service 依赖未安装")
    def test_memory_service_importable(self):
        from backend.service.memory_service import (
            MemoryService, get_memory_service, init_memory_service
        )
        assert MemoryService is not None
        assert callable(get_memory_service)
        assert callable(init_memory_service)


# ── T5-7: store_client 单例验证 ───────────────────────


class TestStoreClientSingleton:
    """T5-7: store_client 单例模式验证（模块文件级检查）。"""

    def test_store_client_exports(self):
        """store_client.py 导出 init_store / close_store / get_store。"""
        src = (_APP_PATH / "backend" / "infra" / "store_client.py").read_text(encoding="utf-8")
        assert "def init_store" in src or "async def init_store" in src
        assert "def close_store" in src or "async def close_store" in src
        assert "def get_store" in src

    def test_store_client_has_singleton_pattern(self):
        """store_client.py 使用单例模式。"""
        src = (_APP_PATH / "backend" / "infra" / "store_client.py").read_text(encoding="utf-8")
        assert "_store_instance" in src, "应使用 _store_instance 全局变量实现单例"


# ── /memories API 路由验证 ─────────────────────────


class TestMemoriesAPI:
    """验证 /memories API 路由已注册。"""

    @pytest.mark.skipif(not _HAS_MEMORY_SERVICE, reason="后端依赖未安装，跳过路由验证")
    def test_memories_route_exists(self):
        try:
            from backend.router import research_router
        except (ImportError, ModuleNotFoundError):
            pytest.skip("fastapi not fully installed in test env")
        routes = [r for r in research_router.routes if hasattr(r, "path")]
        memories_routes = [r for r in routes if "/memories" in r.path]
        assert len(memories_routes) > 0, "GET /memories route should exist"
