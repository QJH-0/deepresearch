"""Phase 1/8 测试：搜索降级——DDG 正常/429 注入/缓存命中/降级链。

覆盖用例:
    T1-5 DDG 正常搜索返回标准结构
    T1-6 DDG 429 注入 → 返回空列表不抛异常
    T1-7 Redis 缓存命中 → 不触发 DDGS().text
    T1-9 web_search_records 降级链（DDG 失败 → SearXNG 兜底）

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


# ──────────────────────────────────────────────
# T1-9 web_search_records 降级链（DDG 失败 → SearXNG 兜底）
# ──────────────────────────────────────────────


class TestWebSearchFallback:
    """web_search_records 链式降级测试（R1.1 改造后基于 SearchProviderChain）。"""

    def test_ddg_success_skips_searxng(self, monkeypatch):
        """DDG 返回结果时不调用 SearXNG。"""
        import mult_agents.tools as tools
        from unittest.mock import AsyncMock, MagicMock

        ddg = MagicMock()
        ddg.available = True
        ddg.search = AsyncMock(return_value=[
            {"title": "ddg", "url": "https://ddg.com", "snippet": "", "source_type": "web"}
        ])
        searxng = MagicMock()
        searxng.available = True
        searxng.search = AsyncMock(return_value=[])

        chain = tools.SearchProviderChain([ddg, searxng])
        monkeypatch.setattr(tools, "_get_provider_chain", lambda: chain)
        tools._reset_provider_chain()

        records = tools.web_search_records("test", count=4)
        assert len(records) == 1
        assert records[0]["title"] == "ddg"
        searxng.search.assert_not_awaited()

    def test_ddg_empty_falls_back_to_searxng(self, monkeypatch):
        """DDG 空结果时降级到 SearXNG。"""
        import mult_agents.tools as tools
        from unittest.mock import AsyncMock, MagicMock

        ddg = MagicMock()
        ddg.available = True
        ddg.search = AsyncMock(return_value=[])
        searxng = MagicMock()
        searxng.available = True
        searxng.search = AsyncMock(return_value=[
            {"title": "searxng", "url": "https://searxng.local", "snippet": "", "source_type": "web"}
        ])

        chain = tools.SearchProviderChain([ddg, searxng])
        monkeypatch.setattr(tools, "_get_provider_chain", lambda: chain)
        tools._reset_provider_chain()

        records = tools.web_search_records("test", count=4)
        assert len(records) == 1
        assert records[0]["title"] == "searxng"

    def test_all_sources_empty_returns_empty(self, monkeypatch):
        """所有源都为空 → 返回空列表。"""
        import mult_agents.tools as tools
        from unittest.mock import AsyncMock, MagicMock

        ddg = MagicMock()
        ddg.available = True
        ddg.search = AsyncMock(return_value=[])
        searxng = MagicMock()
        searxng.available = True
        searxng.search = AsyncMock(return_value=[])

        chain = tools.SearchProviderChain([ddg, searxng])
        monkeypatch.setattr(tools, "_get_provider_chain", lambda: chain)
        tools._reset_provider_chain()

        assert tools.web_search_records("test", count=4) == []


# ──────────────────────────────────────────────
# T1.1 R1.1 Web 搜索 Provider 链式降级新增用例
# ──────────────────────────────────────────────


class TestTavilyProvider:
    """T1.1-01~03 TavilyProvider 测试。"""

    def test_tavily_normalize_output(self):
        """T1.1-01 TavilyProvider 归一化输出【P0】"""
        from mult_agents.tools import TavilyProvider
        from unittest.mock import AsyncMock, MagicMock, patch

        provider = TavilyProvider(api_key="k")
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={
            "results": [{"title": "t", "url": "https://a.com/x", "content": "c"}]
        })

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(provider.search("q", max_results=3))

        assert len(result) == 1
        assert result[0]["source_type"] == "web"
        assert result[0]["domain"] == "a.com"
        assert result[0]["title"] == "t"
        assert result[0]["snippet"] == "c"
        assert result[0]["source_id"] == ""
        assert result[0]["published_at"] == ""

    def test_tavily_no_key_self_disable(self, monkeypatch):
        """T1.1-02 TavilyProvider 无 Key 自禁用【P0】"""
        from mult_agents.tools import TavilyProvider

        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        provider = TavilyProvider()
        assert provider.available is False

        result = asyncio.run(provider.search("q"))
        assert result == []

    def test_tavily_http_exception_returns_empty(self):
        """T1.1-03 Tavily HTTP 异常返回空【P0】"""
        from mult_agents.tools import TavilyProvider
        from unittest.mock import patch

        provider = TavilyProvider(api_key="k")

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(side_effect=Exception("connect error"))
            mock_client_cls.return_value = mock_client
            result = asyncio.run(provider.search("q"))
            assert result == []

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock(side_effect=Exception("500"))
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client
            result = asyncio.run(provider.search("q"))
            assert result == []


class TestSearchProviderChain:
    """T1.1-04~06 SearchProviderChain 链式降级测试。"""

    def test_chain_short_circuit(self):
        """T1.1-04 链式降级顺序短路【P0】"""
        from mult_agents.tools import SearchProviderChain
        from unittest.mock import AsyncMock, MagicMock

        a = MagicMock()
        a.available = True
        a.search = AsyncMock(return_value=[])
        b = MagicMock()
        b.available = True
        b.search = AsyncMock(return_value=[{"title": "b"}])
        c = MagicMock()
        c.available = True
        c.search = AsyncMock(return_value=[{"title": "c"}])

        chain = SearchProviderChain([a, b, c])
        result = asyncio.run(chain.search("q"))
        assert len(result) == 1
        assert result[0]["title"] == "b"
        c.search.assert_not_awaited()

    def test_chain_provider_exception_continues(self):
        """T1.1-05 链内 Provider 抛异常继续降级【P0】"""
        from mult_agents.tools import SearchProviderChain
        from unittest.mock import AsyncMock, MagicMock

        a = MagicMock()
        a.available = True
        a.search = AsyncMock(side_effect=RuntimeError("fail"))
        b = MagicMock()
        b.available = True
        b.search = AsyncMock(return_value=[{"title": "b"}])

        chain = SearchProviderChain([a, b])
        result = asyncio.run(chain.search("q"))
        assert len(result) == 1
        assert result[0]["title"] == "b"

    def test_chain_all_fail_returns_empty(self):
        """T1.1-06 全链失败返回空列表【P0】"""
        from mult_agents.tools import SearchProviderChain
        from unittest.mock import AsyncMock, MagicMock

        providers = []
        for _ in range(3):
            p = MagicMock()
            p.available = True
            p.search = AsyncMock(return_value=[])
            providers.append(p)

        chain = SearchProviderChain(providers)
        assert asyncio.run(chain.search("q")) == []

    def test_chain_filters_unavailable(self):
        """T1.1-07 available=False 的 Provider 被链过滤【P1】"""
        from mult_agents.tools import SearchProviderChain, TavilyProvider
        from unittest.mock import AsyncMock, MagicMock

        tavily = TavilyProvider()
        assert tavily.available is False

        mock_p = MagicMock()
        mock_p.available = True
        mock_p.search = AsyncMock(return_value=[{"title": "ok"}])

        chain = SearchProviderChain([tavily, mock_p])
        assert len(chain._providers) == 1
        result = asyncio.run(chain.search("q"))
        assert len(result) == 1


class TestProviderChainConfig:
    """T1.1-08 配置驱动 Provider 顺序【P1】"""

    def test_config_driven_order(self, monkeypatch):
        import mult_agents.tools as tools
        from unittest.mock import MagicMock

        monkeypatch.setattr(tools, "_load_search_provider_order", lambda: ["searxng", "ddgs"])
        monkeypatch.setenv("SEARX_URL", "http://searx.local")
        tools._reset_provider_chain()
        chain = tools._get_provider_chain()
        names = [type(p).__name__ for p in chain._providers]
        assert names[0] == "SearXNGProvider"
        assert names[1] == "DuckDuckGoProvider"

    def test_config_invalid_falls_back(self):
        """_load_search_provider_order 全非法值时回退默认。"""
        import mult_agents.tools as tools

        monkeypatch_restore = tools._load_search_provider_order
        original = tools._load_search_provider_order

        class FakeSettings:
            search_providers = ["bing", "invalid"]

        import builtins
        original_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "backend.config.settings":
                mod = MagicMock()
                mod.BusinessSettings = lambda: FakeSettings()
                return mod
            return original_import(name, *args, **kwargs)

        builtins.__import__ = fake_import
        try:
            result = tools._load_search_provider_order()
        finally:
            builtins.__import__ = original_import

        assert result == ["ddgs", "searxng"]

    def test_config_empty_falls_back(self):
        """_load_search_provider_order 空列表时回退默认。"""
        import mult_agents.tools as tools
        from unittest.mock import MagicMock

        class FakeSettings:
            search_providers = []

        import builtins
        original_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "backend.config.settings":
                mod = MagicMock()
                mod.BusinessSettings = lambda: FakeSettings()
                return mod
            return original_import(name, *args, **kwargs)

        builtins.__import__ = fake_import
        try:
            result = tools._load_search_provider_order()
        finally:
            builtins.__import__ = original_import

        assert result == ["ddgs", "searxng"]


class TestWebSearchRecordsCompat:
    """T1.1-09 web_search_records 兼容行为回归【P0】"""

    def test_ddg_success_no_searxng(self, monkeypatch):
        import mult_agents.tools as tools
        from unittest.mock import AsyncMock, MagicMock

        ddg = MagicMock()
        ddg.available = True
        ddg.search = AsyncMock(return_value=[
            {"title": "ddg1", "url": "https://ddg.com", "snippet": "", "source_type": "web"},
            {"title": "ddg2", "url": "https://ddg2.com", "snippet": "", "source_type": "web"},
        ])
        searxng = MagicMock()
        searxng.available = True
        searxng.search = AsyncMock(return_value=[
            {"title": "searxng", "url": "https://searxng.local", "snippet": "", "source_type": "web"},
        ])

        chain = tools.SearchProviderChain([ddg, searxng])
        monkeypatch.setattr(tools, "_get_provider_chain", lambda: chain)
        tools._reset_provider_chain()

        records = tools.web_search_records("q", count=5)
        assert len(records) == 2
        searxng.search.assert_not_awaited()

    def test_ddg_empty_uses_searxng(self, monkeypatch):
        import mult_agents.tools as tools
        from unittest.mock import AsyncMock, MagicMock

        ddg = MagicMock()
        ddg.available = True
        ddg.search = AsyncMock(return_value=[])
        searxng = MagicMock()
        searxng.available = True
        searxng.search = AsyncMock(return_value=[
            {"title": "searxng", "url": "https://searxng.local", "snippet": "", "source_type": "web"},
        ])

        chain = tools.SearchProviderChain([ddg, searxng])
        monkeypatch.setattr(tools, "_get_provider_chain", lambda: chain)
        tools._reset_provider_chain()

        records = tools.web_search_records("q", count=5)
        assert len(records) == 1
        assert records[0]["title"] == "searxng"


class TestSearXNGProviderAsync:
    """T1.1-10 SearXNGProvider 异步包装【P1】"""

    def test_searxng_async_wrapper(self, monkeypatch):
        from mult_agents.tools import SearXNGProvider
        from unittest.mock import patch, MagicMock

        provider = SearXNGProvider(base_url="http://searx.local")
        assert provider.available is True

        mock_response = MagicMock()
        mock_response.read = MagicMock(return_value=b'{"results": [{"title": "t", "url": "http://x.com", "content": "c"}]}')
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_response)
        mock_cm.__exit__ = MagicMock(return_value=None)

        with patch("urllib.request.urlopen", return_value=mock_cm):
            result = asyncio.run(provider.search("q", max_results=5))

        assert len(result) == 1
        assert result[0]["title"] == "t"
        assert result[0]["source_type"] == "web"

    def test_searxng_exception_returns_empty(self):
        from mult_agents.tools import SearXNGProvider
        from unittest.mock import patch

        provider = SearXNGProvider(base_url="http://searx.local")

        with patch("urllib.request.urlopen", side_effect=Exception("conn refused")):
            result = asyncio.run(provider.search("q"))
            assert result == []

    def test_searxng_no_url_unavailable(self):
        from mult_agents.tools import SearXNGProvider

        provider = SearXNGProvider(base_url="")
        assert provider.available is False
        assert asyncio.run(provider.search("q")) == []
