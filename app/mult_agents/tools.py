"""工具模块：封装 Web 检索、本地 RAG 查询与通用辅助工具函数。

Web 检索采用多源降级策略（参考 gpt-researcher 项目）：
1. DuckDuckGo（duckduckgo-search >=7.0，免费、无需 API Key）
2. SearXNG（自建实例，环境变量 SEARX_URL 配置；DDG 在国内网络常超时，配置后可作稳定备选）

SearchProvider 抽象层落位后，后续换 Tavily/Bocha/SearXNG 只新增一个 Provider。
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
    """搜索提供者抽象层。后续换 Tavily/Bocha/SearXNG 只新增一个 Provider。"""

    async def search(self, query: str, max_results: int = 6) -> list[dict]:
        """返回标准化的搜索结果记录列表。"""
        ...


class DuckDuckGoProvider:
    """DuckDuckGo 搜索（duckduckgo-search >=7.0）。

    三道限流防线（计划 5.5 节硬性要求）：
    1. max_results=5~8（默认 6），单次检索量收敛
    2. Redis 结果缓存 TTL 1h（key: ddg:{query}）—— 如有 Redis 则启用
    3. 任何异常（含 202/429 限流响应）→ 返回空列表 + 记 warning，绝不抛异常、绝不阻塞主流程
    """

    def __init__(self, redis_client=None):
        self._redis = redis_client  # 可选，无则跳过缓存

    def _ddgs(self):
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        return DDGS()

    async def search(self, query: str, max_results: int = 6) -> list[dict]:
        cache_key = f"ddg:{query}"
        # ② Redis 缓存 TTL 1h
        if self._redis:
            try:
                cached = await self._redis.get(cache_key)
                if cached:
                    return json.loads(cached)
            except Exception:
                pass  # 缓存读取失败不阻塞

        try:
            raw = await asyncio.to_thread(
                lambda: self._ddgs().text(query, max_results=max_results)
            )
            sources = [
                {
                    "source_id": "",
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", ""),
                    "domain": urllib.parse.urlparse(r.get("href", "")).netloc if r.get("href") else "",
                    "source_type": "web",
                    "published_at": "",
                }
                for r in raw
            ]
            if self._redis:
                try:
                    await self._redis.setex(cache_key, 3600, json.dumps(sources, ensure_ascii=False))
                except Exception:
                    pass
            logger.info("[ddg_search] 搜索完成 | query=%s | 记录数=%s", query, len(sources))
            return sources
        except Exception as e:
            # ③ 失败降级：空结果不阻塞
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


def _searxng_search_records(query: str, count: int = 5) -> list[dict]:
    """使用 SearXNG 实例进行搜索（自建免费搜索引擎）。

    参考: gpt-researcher/gpt_researcher/retrievers/searx/searx.py
    """
    searx_url = os.getenv("SEARX_URL", "").strip()
    if not searx_url:
        return []
    if not searx_url.endswith("/"):
        searx_url += "/"
    search_url = urllib.parse.urljoin(searx_url, "search")
    params = {"q": query, "format": "json"}
    try:
        url_with_params = f"{search_url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url_with_params,
            headers={"Accept": "application/json", "User-Agent": "DeepResearch/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception as e:
        logger.error("[searxng_search] 请求失败 | error=%s", e)
        return []

    records: list[dict] = []
    for item in (result.get("results") or [])[:count]:
        url = item.get("url", "")
        domain = ""
        if url.startswith("http"):
            domain = urllib.parse.urlparse(url).netloc
        records.append({
            "source_id": "",
            "title": item.get("title", ""),
            "url": url,
            "snippet": item.get("content", "")[:200],
            "domain": domain,
            "source_type": "web",
            "published_at": "",
        })
    logger.info("[searxng_search] 搜索完成 | 返回记录数=%s", len(records))
    return records

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
    """统一的 Web 搜索入口，采用多源降级策略（参考 gpt-researcher）。

    搜索顺序：
    1. DuckDuckGo（免费、无需 API Key）
    2. SearXNG（如果配置了 SEARX_URL）

    Args:
        query: 搜索关键词
        count: 期望返回的结果数量

    Returns:
        标准化的搜索结果记录列表
    """
    # 策略1: DuckDuckGo（首选）
    records = _ddg_search_records(query, count=count)
    if records:
        logger.info("[web_search] 使用 DuckDuckGo 成功 | 记录数=%s", len(records))
        return records

    # 策略2: SearXNG（自建实例）
    records = _searxng_search_records(query, count=count)
    if records:
        logger.info("[web_search] 使用 SearXNG 成功 | 记录数=%s", len(records))
        return records

    logger.warning("[web_search] 所有搜索源均未返回结果 | query=%s", query)
    return []


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
