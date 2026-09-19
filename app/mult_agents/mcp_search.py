"""通过 MCP 子进程调用 ddgs 搜索。

**为什么走 MCP 而不是直接调库**：ddgs 的 MCP 模式要求 `mcp>=2.0`，而本项目是 Python
项目，`mcp` 与 `fastapi` 共用同一个环境 —— 装 `mcp>=2.0` 会把 `starlette` 拉到 1.6.0，
而 `fastapi 0.123` 要求 `starlette<0.51`，实测直接导致
`TypeError: Router.__init__() got an unexpected keyword argument 'on_startup'`，
后端不可用。

用 `uvx` 启动则让 MCP 服务器跑在**隔离环境**里（uv 自动建 venv），项目环境完全不受影响。
服务器配置见仓库根目录 `mcp_servers.json`（标准 `mcpServers` 格式，可直接复制到其他客户端）。

**每次搜索起一个子进程**：进程生命周期简单、无泄漏风险；代价是 uv 缓存未预热时首次
启动较慢（实测首次约 1 分钟，预热后数秒）。刻意不做常驻会话 —— 那需要在模块级持有
`AsyncExitStack` 并处理跨事件的清理，复杂度与收益不成比例。
"""

import asyncio
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("mult_agents")

MCP_CONFIG_PATH = Path(__file__).resolve().parents[2] / "mcp_servers.json"

# 单次搜索的整体上限：uvx 冷启动 + ddgs 自身多引擎重试都可能很慢
MCP_SEARCH_TIMEOUT_SECONDS = 120.0
MCP_TOOL_NAME = "search_text"


def load_mcp_server_config(name: str = "ddgs") -> dict | None:
    """从 mcp_servers.json 读指定服务器的配置；不存在则返回 None。"""
    try:
        document = json.loads(MCP_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[ddgs-mcp] 读取 %s 失败: %s", MCP_CONFIG_PATH.name, exc)
        return None
    return (document.get("mcpServers") or {}).get(name)


def _build_env(server_config: dict) -> dict:
    """子进程环境 = 当前环境 + 配置里的 env。

    `UV_CACHE_DIR` 支持相对路径（相对仓库根），因为写用户级 uv 缓存可能没权限
    （实测 `failed to write to file ... 拒绝访问 (os error 5)`）。
    """
    env = dict(os.environ)
    for key, value in (server_config.get("env") or {}).items():
        text = str(value)
        if key == "UV_CACHE_DIR" and not Path(text).is_absolute():
            text = str(MCP_CONFIG_PATH.parent / text)
        env[key] = text
    return env


class DdgsMcpProvider:
    """把 ddgs 的 MCP 服务器当作搜索源。"""

    def __init__(self, server_config: dict | None = None):
        self._config = server_config if server_config is not None else load_mcp_server_config()

    @property
    def available(self) -> bool:
        return bool(self._config and self._config.get("command"))

    async def search(self, query: str, max_results: int = 6) -> list[dict]:
        """调用 MCP 的 `search_text` 工具，返回项目统一的记录格式。

        任何异常都降级为空列表 + warning，与链上其他 Provider 一致：
        单个源失败不应中断整条检索链。
        """
        if not self.available:
            logger.warning("[ddgs-mcp] 未配置 MCP 服务器，跳过")
            return []

        try:
            return await asyncio.wait_for(
                self._search_once(query, max_results), timeout=MCP_SEARCH_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            logger.warning("[ddgs-mcp] 超时 %.0fs，降级为空结果 | query=%s",
                           MCP_SEARCH_TIMEOUT_SECONDS, query[:60])
            return []
        except Exception as exc:
            logger.warning("[ddgs-mcp] 失败，降级为空结果 | query=%s | error=%s",
                           query[:60], exc)
            return []

    async def _search_once(self, query: str, max_results: int) -> list[dict]:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=self._config["command"],
            args=list(self._config.get("args") or []),
            env=_build_env(self._config),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    MCP_TOOL_NAME, {"query": query, "max_results": max_results}
                )
        return _parse_tool_result(result, query)


def _parse_tool_result(result, query: str) -> list[dict]:
    """把 MCP 工具返回的内容解析成项目统一的记录格式。

    ⚠️ MCP 把 `-> list[dict]` 的返回值序列化成**多个内容块**（每条结果一个 JSON 对象），
    **不是一个 JSON 数组**。最初按数组解析，明明拿到了真实结果却被判为「无结果」——
    日志里能看到 `payload={'title': 'LangGraph State Machines...'}`。
    因此这里逐块解析：块是 dict 就追加，是 list 就展开。
    """
    payloads: list = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            payloads.extend(parsed)
        elif isinstance(parsed, dict):
            payloads.append(parsed)

    if not payloads:
        # 工具报错时返回的是说明文本，不是结果
        raw = " ".join(
            (getattr(b, "text", "") or "")[:120] for b in (getattr(result, "content", []) or [])
        )
        logger.warning("[ddgs-mcp] 未取到结果 | query=%s | 原始返回=%s", query[:60], raw[:200])
        return []

    records = []
    for item in payloads:
        if not isinstance(item, dict):
            continue
        url = item.get("href") or item.get("url") or ""
        records.append(
            {
                "title": item.get("title", ""),
                "url": url,
                "href": url,
                "snippet": item.get("body") or item.get("snippet") or "",
            }
        )
    return records
