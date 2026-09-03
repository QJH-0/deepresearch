"""Phase 1/8 测试：搜索降级——DDG 正常/429 注入/缓存命中。

覆盖用例:
    T1-5 DDG 正常搜索返回标准结构
    T1-6 DDG 429 注入 → 返回空列表不抛异常
    T1-7 Redis 缓存命中 → 不触发 DDGS().text

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    python -m pytest app/test/test_search_provider.py -v
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
sys.path.insert(0, str(_APP_PATH))


# ──────────────────────────────────────────────
# T1-5 DDG 正常搜索返回标准结构
# ──────────────────────────────────────────────


class TestDuckDuckGoSearch:
    """DuckDuckGoProvider 搜索行为测试。"""

    def test_search_provider_protocol(self):
        """DuckDuckGoProvider 实现 SearchProvider Protocol。"""
        from mult_agents.tools import SearchProvider, DuckDuckGoProvider

        provider = DuckDuckGoProvider()
        assert isinstance(provider, SearchProvider), "DuckDuckGoProvider 应实现 SearchProvider Protocol"

    def test_ddg_search_returns_standard_format(self):
        """mock DDGS 返回标准结构（含 url/title/snippet/source_type）。"""
        from mult_agents.tools import DuckDuckGoProvider

        provider = DuckDuckGoProvider()
        mock_ddgs = MagicMock()
        mock_ddgs.text = MagicMock(return_value=[
            {"title": "Test Result", "href": "https://example.com", "body": "Snippet text"},
        ])
        provider._ddgs = lambda: mock_ddgs

        result = asyncio.run(provider.search("test query", max_results=5))
        assert len(result) == 1
        assert result[0]["title"] == "Test Result"
        assert result[0]["url"] == "https://example.com"
        assert result[0]["snippet"] == "Snippet text"
        assert result[0]["source_type"] == "web"

    def test_ddg_search_empty_results(self):
        """DDGS 返回空列表 → provider 也返回空列表。"""
        from mult_agents.tools import DuckDuckGoProvider

        provider = DuckDuckGoProvider()
        mock_ddgs = MagicMock()
        mock_ddgs.text = MagicMock(return_value=[])
        provider._ddgs = lambda: mock_ddgs

        result = asyncio.run(provider.search("empty", max_results=5))
        assert result == []


# ──────────────────────────────────────────────
# T1-6 DDG 429 注入 → 返回空列表不抛异常
# ──────────────────────────────────────────────


class TestDDGFailureHandling:
    """搜索失败降级测试。"""

    def test_ddg_search_failure_returns_empty(self):
        """mock provider 抛限流异常 → 返回空列表、不抛异常。"""
        from mult_agents.tools import DuckDuckGoProvider

        provider = DuckDuckGoProvider()
        mock_ddgs = MagicMock()
        mock_ddgs.text.side_effect = Exception("429 Too Many Requests")
        provider._ddgs = lambda: mock_ddgs

        result = asyncio.run(provider.search("test query", max_results=5))
        assert result == [], "异常时应返回空列表，不抛异常"

    def test_ddg_search_timeout_returns_empty(self):
        """超时异常也返回空列表。"""
        from mult_agents.tools import DuckDuckGoProvider

        provider = DuckDuckGoProvider()
        mock_ddgs = MagicMock()
        mock_ddgs.text.side_effect = TimeoutError("Connection timed out")
        provider._ddgs = lambda: mock_ddgs

        result = asyncio.run(provider.search("timeout test", max_results=5))
        assert result == []

    def test_ddg_search_generic_exception_returns_empty(self):
        """任意异常都返回空列表。"""
        from mult_agents.tools import DuckDuckGoProvider

        provider = DuckDuckGoProvider()
        mock_ddgs = MagicMock()
        mock_ddgs.text.side_effect = RuntimeError("Unexpected error")
        provider._ddgs = lambda: mock_ddgs

        result = asyncio.run(provider.search("error test", max_results=5))
        assert result == []


# ──────────────────────────────────────────────
# T1-7 Redis 缓存命中
# ──────────────────────────────────────────────


class TestDDGCacheHit:
    """Redis 缓存命中场景。"""

    def test_ddg_cache_hit(self):
        """同 query 二次调用不触发 DDGS().text（缓存命中）。"""
        from mult_agents.tools import DuckDuckGoProvider

        mock_redis = MagicMock()
        cached_data = [
            {"title": "cached", "url": "http://cached.com", "snippet": "", "source_type": "web"}
        ]
        mock_redis.get = AsyncMock(
            return_value='[{"title": "cached", "url": "http://cached.com", "snippet": "", "source_type": "web"}]'
        )
        mock_redis.setex = AsyncMock()

        provider = DuckDuckGoProvider(redis_client=mock_redis)

        # 第一次调用（缓存命中）
        result = asyncio.run(provider.search("test", max_results=5))
        assert len(result) == 1
        assert result[0]["title"] == "cached"

        # DDGS().text 不应被调用
        mock_ddgs = MagicMock()
        mock_ddgs.text = MagicMock(return_value=[])
        provider._ddgs = lambda: mock_ddgs

        result2 = asyncio.run(provider.search("test", max_results=5))
        assert mock_ddgs.text.call_count == 0, "缓存命中时不应调用 DDGS().text()"

    def test_ddg_cache_miss_triggers_search(self):
        """缓存未命中 → 触发 DDGS().text。"""
        from mult_agents.tools import DuckDuckGoProvider

        mock_redis = MagicMock()
        mock_redis.get = AsyncMock(return_value=None)  # 缓存未命中
        mock_redis.setex = AsyncMock()

        provider = DuckDuckGoProvider(redis_client=mock_redis)
        mock_ddgs = MagicMock()
        mock_ddgs.text = MagicMock(return_value=[
            {"title": "fresh", "href": "http://fresh.com", "body": "fresh snippet"},
        ])
        provider._ddgs = lambda: mock_ddgs

        result = asyncio.run(provider.search("fresh query", max_results=5))
        assert len(result) == 1
        assert result[0]["title"] == "fresh"
        assert mock_ddgs.text.call_count == 1, "缓存未命中应调用 DDGS().text()"

    def test_ddg_no_redis_skips_cache(self):
        """无 Redis → 跳过缓存，直接搜索。"""
        from mult_agents.tools import DuckDuckGoProvider

        provider = DuckDuckGoProvider(redis_client=None)
        mock_ddgs = MagicMock()
        mock_ddgs.text = MagicMock(return_value=[
            {"title": "no-cache", "href": "http://n.com", "body": "text"},
        ])
        provider._ddgs = lambda: mock_ddgs

        result = asyncio.run(provider.search("no cache", max_results=5))
        assert len(result) == 1
        assert mock_ddgs.text.call_count == 1
