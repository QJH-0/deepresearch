"""工具模块：封装 Web 检索、本地 RAG 查询与通用辅助工具函数。

Web 检索采用可配置的 Provider 链式降级策略（参考 gpt-researcher 项目）：
按 config.json 的 search_providers 顺序依次尝试，任一 Provider 成功即返回。
内置 ddgs / tavily / searxng 三个 Provider，可通过配置调整顺序与启停。
"""

from datetime import datetime
import ast
import json
import logging
import operator
import os
from pathlib import Path
import urllib.parse
import urllib.request

from langchain_core.tools import tool
from typing import Optional
from .rag.core import RAGSystem, RAGConfig

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

    def _ddgs(self):
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        return DDGS()

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


def _ddg_search_records(query: str, count: int = 6) -> list[dict]:
    """同步包装的 DuckDuckGo 搜索（供 web_search_records 调用）。"""
    import asyncio as _asyncio
    try:
        loop = _asyncio.get_event_loop()
        if loop.is_running():
            # 已在事件循环中，用 run_until_complete 会报错——用新线程
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(
                    lambda: _asyncio.run(_get_ddg_provider().search(query, max_results=count))
                ).result()
    except RuntimeError:
        pass
    return _asyncio.run(_get_ddg_provider().search(query, max_results=count))


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


def _searxng_search_records(query: str, count: int = 5) -> list[dict]:
    """同步包装 SearXNG 搜索（兼容旧测试调用方）。"""
    provider = SearXNGProvider()
    if not provider.available:
        return []
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(
                lambda: asyncio.run(provider.search(query, max_results=count))
            ).result()
    return asyncio.run(provider.search(query, max_results=count))


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


_VALID_PROVIDER_NAMES = {"ddgs", "tavily", "searxng"}


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
        from backend.config.settings import BusinessSettings
        order = BusinessSettings().search_providers
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
            "tavily": lambda: TavilyProvider(),
            "searxng": lambda: SearXNGProvider(os.getenv("SEARX_URL", "").strip()),
        }
        providers = [factories[name]() for name in order if name in factories]
        _PROVIDER_CHAIN = SearchProviderChain(providers)
    return _PROVIDER_CHAIN


def _reset_provider_chain():
    """重置全局链单例（供测试使用）。"""
    global _PROVIDER_CHAIN
    _PROVIDER_CHAIN = None


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
    if _RAG_SYSTEM is None:
        return []
    try:
        return _RAG_SYSTEM.search_records(query, k=limit)
    except Exception:
        return []


def web_search_records(query: str, count: int = 5) -> list[dict]:
    """统一的 Web 搜索入口，采用可配置的 Provider 链式降级策略。

    按 config.json 的 search_providers 顺序依次尝试，任一 Provider 成功即返回。
    保持同步签名（调用方 web_search_node 为同步函数），内部通过专用后台事件循环
    线程运行异步 Provider 链。
    """
    chain = _get_provider_chain()
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
        return future.result(timeout=60)
    return asyncio.run(chain.search(query, max_results=count))


@tool
def search_knowledge_base(query: str) -> str:
    """
    查询本地知识库/向量数据库。
    当用户询问关于专业知识、历史文档或私有数据时使用此工具。
    输入应该是具体的查询问题。
    """
    if _RAG_SYSTEM is None:
        return "错误：RAG 系统未初始化或连接失败。请检查 Milvus 服务状态。"
    return _RAG_SYSTEM.search(query)


ALLOWED_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
}


def _eval_node(node):
    if isinstance(node, ast.Num):
        return node.n
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in ALLOWED_OPERATORS:
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        return ALLOWED_OPERATORS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_node(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value
    raise ValueError("Unsupported expression")


@tool
def get_current_time() -> str:
    """返回当前时间的 ISO 字符串。"""
    return datetime.now().isoformat()


@tool
def simple_calculator(expression: str) -> str:
    """计算简单算术表达式并返回结果。"""
    tree = ast.parse(expression, mode="eval")
    result = _eval_node(tree.body)
    return str(result)


@tool
def extract_requirements(text: str) -> str:
    """从文本中提取需求要点列表。"""
    items = [part.strip() for part in text.replace("\n", " ").split("。") if part.strip()]
    return "\n".join(f"- {item}" for item in items[:8])


@tool
def outline_from_topics(topics: str) -> str:
    """根据主题列表生成编号大纲。"""
    raw = topics.replace("\n", ",")
    items = [item.strip() for item in raw.split(",") if item.strip()]
    return "\n".join(f"{idx+1}. {item}" for idx, item in enumerate(items[:10]))


@tool
def merge_notes(note_a: str, note_b: str) -> str:
    """合并两段文本为一段笔记。"""
    return f"{note_a}\n{note_b}".strip()


@tool
def summarize_points(text: str) -> str:
    """从文本中抽取要点列表。"""
    sentences = [s.strip() for s in text.replace("\n", " ").split("。") if s.strip()]
    points = sentences[:6]
    return "\n".join(f"- {p}" for p in points)


@tool
def dedupe_lines(text: str) -> str:
    """对文本按行去重并输出。"""
    seen = set()
    lines = []
    for line in text.splitlines():
        key = line.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        lines.append(line)
    return "\n".join(lines)


@tool
def web_search_stub(query: str) -> str:
    """网络检索接口（DuckDuckGo / SearXNG 多源降级）。"""
    records = web_search_records(query, count=5)
    if not records:
        return "网络检索未返回结果，请检查网络连接或配置 SEARX_URL。"
    lines = ["网络检索结果："]
    for idx, record in enumerate(records, 1):
        lines.append(f"{idx}. {record['title']}")
        url = record.get("url", "")
        if url:
            lines.append(f"   链接: {url}")
        snippet = record.get("snippet", "")
        if snippet:
            lines.append(f"   摘要: {snippet[:200]}")
    return "\n".join(lines)


@tool
def local_docs_lookup_stub(query: str) -> str:
    """模拟本地检索接口。"""
    return f"未配置本地检索服务，收到查询: {query}"


@tool
def local_vector_search_stub(query: str) -> str:
    """模拟向量数据库检索接口。"""
    return f"未配置向量数据库，收到查询: {query}"


@tool
def optimize_query(query: str) -> str:
    """对检索问题进行改写与优化。"""
    return f"优化后的查询建议: {query}"


@tool
def explain_term(term: str) -> str:
    """解释领域术语。"""
    return f"{term} 需要结合上下文进一步解释"


@tool
def python_inter(code: str) -> str:
    """模拟 Python 执行环境。"""
    return f"未配置Python执行环境，收到代码: {code}"


@tool
def fig_inter(spec: str) -> str:
    """模拟绘图执行环境。"""
    return f"未配置绘图环境，收到图表需求: {spec}"


@tool
def amap_weather(city: str) -> str:
    """模拟高德天气查询。"""
    return f"未配置高德API，收到天气查询: {city}"


@tool
def amap_geocode(address: str) -> str:
    """模拟高德地理编码。"""
    return f"未配置高德API，收到地理编码请求: {address}"


@tool
def amap_poi_search(query: str) -> str:
    """模拟高德 POI 检索。"""
    return f"未配置高德API，收到POI检索: {query}"


@tool
def amap_route_plan(origin: str, destination: str) -> str:
    """模拟高德路径规划。"""
    return f"未配置高德API，收到路径规划: {origin} -> {destination}"


def _workspace_root() -> Path:
    base = os.getenv("WORKSPACE_DIR", "/workspace")
    return Path(base).resolve()


def _safe_path(path: str) -> Path:
    root = _workspace_root()
    target = (root / path).resolve()
    if root not in target.parents and target != root:
        raise ValueError("路径超出工作目录")
    return target


@tool
def safe_list_dir(path: str = ".") -> str:
    """安全列出工作目录下的文件与子目录。"""
    root = _workspace_root()
    if not root.exists():
        return f"工作目录不存在: {root}"
    target = _safe_path(path)
    if not target.exists() or not target.is_dir():
        return "目录不存在"
    items = [p.name for p in target.iterdir()]
    return "\n".join(items)


@tool
def safe_read_file(path: str) -> str:
    """安全读取工作目录内的文件。"""
    root = _workspace_root()
    if not root.exists():
        return f"工作目录不存在: {root}"
    target = _safe_path(path)
    if not target.exists() or not target.is_file():
        return "文件不存在"
    return target.read_text(encoding="utf-8")


@tool
def safe_write_file(path: str, content: str) -> str:
    """安全写入工作目录内的文件。"""
    root = _workspace_root()
    if not root.exists():
        return f"工作目录不存在: {root}"
    target = _safe_path(path)
    if not target.parent.exists():
        return "目录不存在"
    target.write_text(content, encoding="utf-8")
    return f"已写入: {target}"


@tool
def safe_move_file(src: str, dst: str) -> str:
    """安全移动工作目录内的文件。"""
    root = _workspace_root()
    if not root.exists():
        return f"工作目录不存在: {root}"
    src_path = _safe_path(src)
    dst_path = _safe_path(dst)
    if not src_path.exists():
        return "源文件不存在"
    if not dst_path.parent.exists():
        return "目标目录不存在"
    src_path.replace(dst_path)
    return f"已移动: {dst_path}"


@tool
def sql_inter(query: str) -> str:
    """模拟 SQL 执行接口。"""
    return f"未配置数据库，收到SQL: {query}"


@tool
def extract_data_stub(query: str) -> str:
    """模拟数据抽取接口。"""
    return f"未配置数据抽取环境，收到请求: {query}"


@tool
def execute_terminal_command(command: str) -> str:
    """模拟终端命令执行接口。"""
    return f"未配置终端执行环境，收到命令: {command}"


@tool
def file_operation_stub(request: str) -> str:
    """模拟文件操作接口。"""
    return f"未配置文件操作环境，收到请求: {request}"


@tool
def news_search_stub(query: str) -> str:
    """模拟新闻检索接口。"""
    return f"未配置新闻检索服务，收到查询: {query}"


@tool
def finance_search_stub(query: str) -> str:
    """模拟金融检索接口。"""
    return f"未配置金融检索服务，收到查询: {query}"


@tool
def extract_url_content_stub(url: str) -> str:
    """模拟 URL 内容抽取接口。"""
    return f"未配置URL解析服务，收到URL: {url}"
