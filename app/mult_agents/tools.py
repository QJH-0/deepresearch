"""工具模块：Web 检索 Provider 链、本地 RAG 检索入口、网页正文抓取。

Web 检索采用可配置的 Provider 链式降级策略（参考 gpt-researcher 项目）：
按 config.json 的 search_providers 顺序依次尝试，任一 Provider 成功即返回。
内置 ddgs / tavily / searxng 三个 Provider，可通过配置调整顺序与启停。

**哪些走直调、哪些绑给 agent**（Anthropic 的判据：任务路径可预测就用 workflow）：

- `web_search_records` / `search_knowledge_base_records`：节点**直调**。
  「已知必须检索」的路径可预测，workflow 比 agent 更可控、更便宜。
- `fetch_url_tool`：**绑定给 `deep_dive` 的 agent**。
  「哪些证据可疑、要不要读原文核实」是运行时才知道的，属 agent 适用场景。
"""

import ipaddress
import json
import logging
import os
import re
import socket
import threading
import urllib.parse
import urllib.request
from concurrent.futures import TimeoutError as FuturesTimeoutError
from html.parser import HTMLParser
from typing import Optional

import httpx
from langchain_core.tools import tool

from .mcp_search import DdgsMcpProvider
from .rag.core import RAGConfig, RAGSystem

logger = logging.getLogger("mult_agents")

# ── DuckDuckGo 搜索（P1-5：主搜索源，三道限流防线）──

import asyncio
from typing import Protocol, runtime_checkable


@runtime_checkable
class SearchProvider(Protocol):
    """搜索提供者抽象层。后续换 Tavily/SearXNG 只新增一个 Provider。"""

    async def search(self, query: str, max_results: int = 6) -> list[dict]:
        """返回标准化的搜索结果记录列表。"""
        ...


def _normalize_web_record(title: str, url: str, snippet: str) -> dict:
    """将各搜索源的原始结果归一化为标准记录结构。"""
    return {
        "source_id": "",
        "title": title,
        "url": url,
        "snippet": snippet,
        "domain": urllib.parse.urlparse(url).netloc if url else "",
        "source_type": "web",
        "published_at": "",
    }


class DuckDuckGoProvider:
    """DuckDuckGo 搜索（duckduckgo-search >=7.0）。

    三道限流防线：
    1. max_results=5~8（默认 6），单次检索量收敛
    2. Redis 结果缓存 TTL 1h（key: ddg:{query}）—— 如有 Redis 则启用
    3. 任何异常（含 202/429 限流响应）→ 返回空列表 + 记 warning，绝不抛异常、绝不阻塞主流程
    """

    def __init__(self, redis_client=None):
        self._redis = redis_client

    @property
    def available(self) -> bool:
        return True

    @staticmethod
    def _resolve_proxy() -> str | None:
        """解析 ddgs 该用的代理。

        背景（**此处曾有过一个错误结论，务必看完**）：
        我曾以为「ddgs 不读 `HTTP_PROXY`」，据此把它当 bug 修。**这个判断是错的。**
        ddgs 底层的 `primp` 客户端**确实会读 `HTTP_PROXY`/`HTTPS_PROXY`**
        （实测：代理指向死端口时，`primp.Client()` 2.05s 快速 ConnectError；
        设与不设代理时 ddgs 的异常类型与耗时都明显不同）。

        显式传参的真实价值是另外两条：
        1. 支持 `DDGS_PROXY` —— ddgs 官方文档里那个变量，`primp` 不认它；
        2. 代理来源在代码里可见，排查时不用去猜底层客户端读到了什么。

        优先级：`DDGS_PROXY`（ddgs 自己的变量）> `HTTPS_PROXY` > `HTTP_PROXY`。
        不看 `NO_PROXY`：ddgs 只访问搜索引擎，没有「不该走代理」的目标。
        """
        for key in ("DDGS_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            value = (os.environ.get(key) or "").strip()
            if value:
                return value
        return None

    def _ddgs(self):
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        # 不传 timeout：ddgs 的 timeout 是**单次 HTTP 请求**上限，
        # 而 search_timeout_seconds 是**整条 Provider 链**的上限，两者作用域不同。
        # 把后者透传进来会拉长每次请求的等待，与「源不可达时快速失败」的初衷相反。
        return DDGS(proxy=self._resolve_proxy())

    async def search(self, query: str, max_results: int = 6) -> list[dict]:
        cache_key = f"ddg:{query}"
        if self._redis:
            try:
                cached = await self._redis.get(cache_key)
                if cached:
                    return json.loads(cached)
            except Exception:
                pass

        try:
            raw = await asyncio.to_thread(
                lambda: self._ddgs().text(query, max_results=max_results)
            )
            sources = [_normalize_web_record(
                title=r.get("title", ""),
                url=r.get("href", ""),
                snippet=r.get("body", ""),
            ) for r in raw]
            if self._redis:
                try:
                    await self._redis.setex(cache_key, 3600, json.dumps(sources, ensure_ascii=False))
                except Exception:
                    pass
            logger.info("[ddg_search] 搜索完成 | query=%s | 记录数=%s", query, len(sources))
            return sources
        except Exception as e:
            logger.warning("[ddg_search] 搜索失败，降级为空结果 | query=%s | error=%s", query, e)
            return []


# 全局 DuckDuckGo provider 实例
_DDG_PROVIDER: DuckDuckGoProvider | None = None


def _get_ddg_provider() -> DuckDuckGoProvider:
    global _DDG_PROVIDER
    if _DDG_PROVIDER is None:
        _DDG_PROVIDER = DuckDuckGoProvider()
    return _DDG_PROVIDER




# ── SearXNG 搜索（参考 gpt-researcher/retrievers/searx）──


class SearXNGProvider:
    """SearXNG 自建搜索引擎 Provider（urllib 同步实现，asyncio.to_thread 转异步）。"""

    def __init__(self, base_url: str | None = None, timeout: float = 15.0):
        self._base_url = (base_url or os.getenv("SEARX_URL", "")).strip()
        self._timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self._base_url)

    def _search_sync(self, query: str, max_results: int) -> list[dict]:
        searx_url = self._base_url
        if not searx_url.endswith("/"):
            searx_url += "/"
        search_url = urllib.parse.urljoin(searx_url, "search")
        params = {"q": query, "format": "json"}
        url_with_params = f"{search_url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url_with_params,
            headers={"Accept": "application/json", "User-Agent": "DeepResearch/1.0"},
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as response:
            result = json.loads(response.read().decode("utf-8"))

        return [_normalize_web_record(
            title=item.get("title", ""),
            url=item.get("url", ""),
            snippet=item.get("content", "")[:200],
        ) for item in (result.get("results") or [])[:max_results]]

    async def search(self, query: str, max_results: int = 6) -> list[dict]:
        if not self.available:
            return []
        try:
            records = await asyncio.to_thread(self._search_sync, query, max_results)
            logger.info("[searxng_search] 搜索完成 | 返回记录数=%s", len(records))
            return records
        except Exception as e:
            logger.warning("[searxng_search] 请求失败 | error=%s", e)
            return []




# ── Tavily 商用搜索兜底 ──


class TavilyProvider:
    """Tavily 商用搜索兜底（API Key 缺失时自禁用）。"""

    def __init__(self, api_key: str | None = None, timeout: float = 10.0):
        self._api_key = (api_key or os.getenv("TAVILY_API_KEY", "")).strip()
        self._timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    async def search(self, query: str, max_results: int = 6) -> list[dict]:
        if not self.available:
            return []
        try:
            import httpx
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": self._api_key,
                        "query": query,
                        "max_results": max_results,
                        "search_depth": "basic",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("[tavily] 搜索失败 | query=%s | error=%s", query, exc)
            return []
        return [_normalize_web_record(
            title=r.get("title", ""),
            url=r.get("url", ""),
            snippet=r.get("content", ""),
        ) for r in (data.get("results") or [])[:max_results]]


# ── Provider 链式降级管理器 ──


_VALID_PROVIDER_NAMES = {"ddgs", "ddgs_mcp", "tavily", "searxng"}


class SearchProviderChain:
    """按注册顺序链式降级：任一 Provider 返回非空结果即短路返回。"""

    def __init__(self, providers: list):
        self._providers = [p for p in providers if getattr(p, "available", True)]

    async def search(self, query: str, max_results: int = 6) -> list[dict]:
        for provider in self._providers:
            try:
                records = await provider.search(query, max_results=max_results)
            except Exception as exc:
                logger.warning("[search-chain] %s 异常 | query=%s | error=%s",
                               type(provider).__name__, query, exc)
                records = []
            if records:
                return records
            logger.info("[search-chain] %s 无结果，尝试下一 Provider", type(provider).__name__)
        logger.warning("[search-chain] 所有搜索源均未返回结果 | query=%s", query)
        return []


_PROVIDER_CHAIN: SearchProviderChain | None = None
_CHAIN_LOOP = None


def _get_chain_loop():
    """专用后台事件循环线程，用于同步上下文调用异步 Provider 链。"""
    global _CHAIN_LOOP
    if _CHAIN_LOOP is not None and not _CHAIN_LOOP.is_closed():
        return _CHAIN_LOOP
    import threading
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    _CHAIN_LOOP = loop
    return _CHAIN_LOOP


def _load_search_provider_order() -> list[str]:
    """从 config.json 读取 search_providers 配置，非法值自动剔除。"""
    try:
        from backend.config.settings import get_business_settings
        order = get_business_settings().search_providers
    except Exception:
        order = ["ddgs", "searxng"]
    valid = [name for name in order if name in _VALID_PROVIDER_NAMES]
    if not valid:
        valid = ["ddgs", "searxng"]
    return valid


def _get_provider_chain() -> SearchProviderChain:
    """按 config.json 的 search_providers 顺序构建全局链（惰性单例）。"""
    global _PROVIDER_CHAIN
    if _PROVIDER_CHAIN is None:
        order = _load_search_provider_order()
        factories = {
            "ddgs": lambda: _get_ddg_provider(),
            # ddgs 的 MCP 模式：子进程跑在 uvx 隔离环境里，见 mcp_servers.json
            "ddgs_mcp": lambda: DdgsMcpProvider(),
            "tavily": lambda: TavilyProvider(),
            "searxng": lambda: SearXNGProvider(os.getenv("SEARX_URL", "").strip()),
        }
        built = [(name, factories[name]()) for name in order if name in factories]
        providers = [p for _name, p in built if getattr(p, "available", True)]
        _PROVIDER_CHAIN = SearchProviderChain(providers)

        # 启动期把「哪些搜索源真正可用」讲清楚：否则用户会在跑完一次十几分钟的
        # 研究流程后才从报告里发现「网页检索命中 0 条」，而根因只是缺个 API Key。
        active = [type(p).__name__ for p in providers]
        skipped = [name for name, p in built if not getattr(p, "available", True)]
        if active:
            logger.info("[search-chain] 可用搜索源: %s", ", ".join(active))
        if skipped:
            logger.warning(
                "[search-chain] 以下搜索源因未配置而跳过: %s"
                "（tavily 需 TAVILY_API_KEY，searxng 需 SEARX_URL）",
                ", ".join(skipped),
            )
        if not active:
            logger.error(
                "[search-chain] 没有任何可用的搜索源，网页检索将始终返回 0 条。"
                "请配置 TAVILY_API_KEY 或 SEARX_URL 后重启。"
            )
    return _PROVIDER_CHAIN


def _reset_provider_chain():
    """重置全局链单例（供测试使用）。"""
    global _PROVIDER_CHAIN
    _PROVIDER_CHAIN = None


# R3.4: 配置热更后重置搜索链单例
try:
    from backend.config.settings import register_reload_callback
    register_reload_callback(_reset_provider_chain)
except Exception:
    pass


# 全局 RAG 系统实例
_RAG_SYSTEM: Optional[RAGSystem] = None

def init_rag_system(api_key: str, config: Optional[RAGConfig] = None) -> None:
    """初始化全局 RAG 系统。

    Raises:
        RuntimeError: RAG 系统初始化失败时抛出，包含原始异常链。
    """
    global _RAG_SYSTEM
    if _RAG_SYSTEM is None:
        try:
            _RAG_SYSTEM = RAGSystem(api_key, config)
        except Exception as e:
            logger.error("RAG 系统初始化失败: %s", e, exc_info=True)
            raise RuntimeError(f"RAG 系统初始化失败: {e}") from e


def search_knowledge_base_records(query: str, limit: int = 5) -> list[dict]:
    """本地知识库检索入口（节点直调，同步）。

    失败时返回空列表以免中断研究链路，但**必须留日志** ——
    静默返回空会让「检索不到」与「检索坏了」无法区分，
    最终表现为报告里一句「本地检索命中 0 条」而没有任何线索。
    """
    if _RAG_SYSTEM is None:
        logger.warning("[local_rag] RAG 系统未初始化，本地检索返回空 | query=%s", query[:60])
        return []
    try:
        return _RAG_SYSTEM.search_records(query, k=limit)
    except Exception as exc:
        logger.warning(
            "[local_rag] 本地检索失败，返回空结果 | query=%s | %s: %s",
            query[:60], type(exc).__name__, exc,
        )
        return []


def _search_timeout_seconds() -> float:
    """单次检索（含整条 Provider 链）的时间上限。

    搜索源不可达时，每个查询都要空等满超时才降级 —— 6 个查询就是 6 分钟纯等待，
    用户只看到界面转圈。默认 15 秒是「单源可用时够用、不可达时快速失败」的折中。
    """
    try:
        from backend.config.settings import get_business_settings

        return float(get_business_settings().search_timeout_seconds)
    except Exception:
        return 15.0


def web_search_records(query: str, count: int = 5) -> list[dict]:
    """统一的 Web 搜索入口，采用可配置的 Provider 链式降级策略。

    按 config.json 的 search_providers 顺序依次尝试，任一 Provider 成功即返回。
    保持同步签名（调用方 web_search_node 为同步函数），内部通过专用后台事件循环
    线程运行异步 Provider 链。

    超时一律降级为空结果并告警，与 Provider 自身的失败处理保持一致 ——
    检索不到东西是合法场景，不该让整轮研究卡死。
    """
    chain = _get_provider_chain()
    timeout = _search_timeout_seconds()
    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False

    if in_loop:
        loop = _get_chain_loop()
        future = asyncio.run_coroutine_threadsafe(
            chain.search(query, max_results=count), loop
        )
        try:
            return future.result(timeout=timeout)
        except FuturesTimeoutError:
            # 链跑在常驻后台循环里，超时后必须显式取消，
            # 否则挂起的检索会连同连接一起留在那个循环上
            future.cancel()
            logger.warning(
                "[search-chain] 检索超时 %.0fs，降级为空结果 | query=%s", timeout, query[:60]
            )
            return []

    try:
        return asyncio.run(
            asyncio.wait_for(chain.search(query, max_results=count), timeout=timeout)
        )
    except (asyncio.TimeoutError, FuturesTimeoutError):
        logger.warning(
            "[search-chain] 检索超时 %.0fs，降级为空结果 | query=%s", timeout, query[:60]
        )
        return []


# ══════════════════════════════════════════════════════════════
# 网页正文抓取：唯一一个绑定给 agent 的工具
# ══════════════════════════════════════════════════════════════
#
# 与上面几个入口的区别：`web_search_records` / `search_knowledge_base_records`
# 仍由节点**直调**（任务路径可预测，workflow 比 agent 更可控更便宜）；
# 只有 `fetch_url` 绑给 agent —— 「哪些证据可疑、要不要读原文」是运行时才知道的，
# 符合 Anthropic 对「何时该用 agent」的判据。

_FETCH_TIMEOUT_SECONDS = 20.0
_FETCH_MAX_CHARS = 8000
_FETCH_USER_AGENT = "Mozilla/5.0 (compatible; DeepResearch/1.0)"

# 非正文区块：丢掉能显著提升正文密度，也避免把脚本/样式当证据
_FETCH_DROP_TAGS = frozenset({
    "script", "style", "noscript", "nav", "header", "footer",
    "aside", "form", "svg", "iframe", "template",
})
_FETCH_BLOCK_TAGS = frozenset({
    "p", "br", "div", "li", "tr", "section", "article", "blockquote",
    "h1", "h2", "h3", "h4", "h5", "h6",
})


class _HtmlTextExtractor(HTMLParser):
    """把 HTML 抽成纯文本。用标准库而非 bs4：bs4 只是传递依赖，未在 requirements 声明。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._drop_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _FETCH_DROP_TAGS:
            self._drop_depth += 1
        elif tag in _FETCH_BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _FETCH_DROP_TAGS and self._drop_depth:
            self._drop_depth -= 1
        elif tag in _FETCH_BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._drop_depth and data.strip():
            self._chunks.append(data)

    @property
    def text(self) -> str:
        joined = "".join(self._chunks)
        joined = re.sub(r"[ \t\r\f\v]+", " ", joined)
        return re.sub(r"\n\s*\n\s*\n+", "\n\n", joined).strip()


_DNS_TIMEOUT_SECONDS = 5.0


def _resolve_host(host: str, timeout: float = _DNS_TIMEOUT_SECONDS):
    """带超时的 DNS 解析；超时返回 None。

    用**守护线程 + join(timeout)** 而非 ThreadPoolExecutor：后者的
    `__exit__` 会 `shutdown(wait=True)`，等于把超时取消掉。
    守护线程即使挂住也不会阻塞进程退出。
    """
    outcome: dict = {}

    def worker() -> None:
        try:
            outcome["infos"] = socket.getaddrinfo(host, None)
        except OSError as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout)
    return outcome.get("infos")


def _is_public_host(host: str) -> bool:
    """SSRF 防护：解析到的所有地址都必须是公网，否则拒绝。

    只做「域名白名单」不够 —— 攻击者可以用指向 169.254.169.254 的域名绕过。

    解析**必须带超时**：`socket.getaddrinfo` 在解析器无响应时会长时间挂住，
    而这里没有任何超时保护 —— 挂住的不是一次请求，是整个 `deep_dive` 节点。
    """
    infos = _resolve_host(host)
    if not infos:
        return False
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_reserved or address.is_multicast or address.is_unspecified):
            return False
    return True


def fetch_url(url: str, max_chars: int = _FETCH_MAX_CHARS) -> str:
    """抓取网页正文并转成纯文本。

    返回带前缀的字符串而非抛异常：工具的输出会直接进 agent 的上下文，
    「失败原因」本身就是 agent 决定下一步的依据，抛异常只会打断循环。
    """
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        return f"[抓取失败] 只支持 http/https，收到: {parsed.scheme or '(空)'}"
    if not parsed.hostname:
        return "[抓取失败] URL 缺少主机名"
    if not _is_public_host(parsed.hostname):
        logger.warning("[fetch_url] 拒绝非公网目标 | url=%s", url[:120])
        return "[抓取失败] 目标主机不在公网（内网/回环地址一律拒绝）"

    try:
        response = httpx.get(
            url,
            timeout=_FETCH_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": _FETCH_USER_AGENT},
        )
    except Exception as exc:
        logger.warning("[fetch_url] 请求失败 | url=%s | error=%s", url[:120], exc)
        return f"[抓取失败] 请求异常: {type(exc).__name__}: {exc}"

    if response.status_code >= 400:
        logger.warning("[fetch_url] HTTP %s | url=%s", response.status_code, url[:120])
        return f"[抓取失败] HTTP {response.status_code}"

    content_type = (response.headers.get("content-type") or "").lower()
    body = response.text or ""
    if "html" in content_type or body.lstrip()[:1] == "<":
        extractor = _HtmlTextExtractor()
        try:
            extractor.feed(body)
        except Exception as exc:  # 解析异常不应让整条链路失败
            logger.warning("[fetch_url] HTML 解析异常，改用原始文本 | %s", exc)
            text = body
        else:
            text = extractor.text or body
    else:
        text = body

    text = text.strip()
    if not text:
        return "[抓取失败] 正文为空"
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n...[已截断，原文共 {len(text)} 字]"
    logger.info("[fetch_url] 抓取成功 | url=%s | 字数=%s", url[:120], len(text))
    return text


@tool
def fetch_url_tool(url: str) -> str:
    """抓取指定网页的正文（已转纯文本），用于核实证据原文。

    何时使用：某条证据的摘要（snippet）与结论相关但不足以确认，或两条证据互相冲突、
    需要看原文才能判断时。不要对已经足够清楚的证据重复抓取，也不要一次抓取超过 3 个 URL。

    Args:
        url: 要抓取的网页地址，必须是 http/https 开头且可从证据的 url 字段取得。
    """
    return fetch_url(url)
