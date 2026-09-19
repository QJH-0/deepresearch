"""ddgs MCP 搜索 Provider 的测试。

重点锁住一个真实踩过的 bug：**MCP 把 `-> list[dict]` 的返回值序列化成多个内容块
（每条结果一个 JSON 对象），不是一个 JSON 数组**。最初按数组解析，明明拿到了真实结果
（日志里能看到 `payload={'title': 'LangGraph State Machines...'}`）却被判为「无结果」。

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    python -m pytest app/test/test_mcp_search.py -v
"""

import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "app"))

from mult_agents import mcp_search  # noqa: E402


class _Block:
    def __init__(self, text):
        self.text = text


class _Result:
    def __init__(self, blocks):
        self.content = [_Block(b) for b in blocks]


class TestParseToolResult:
    def test_parses_one_json_object_per_content_block(self):
        """真实格式：每条结果一个内容块。这是踩过的坑，必须锁住。"""
        result = _Result([
            json.dumps({"title": "A", "href": "https://a.example", "body": "sa"}),
            json.dumps({"title": "B", "href": "https://b.example", "body": "sb"}),
        ])

        rows = mcp_search._parse_tool_result(result, "q")

        assert [r["title"] for r in rows] == ["A", "B"], (
            "MCP 逐条返回内容块，按 JSON 数组解析会把结果全丢掉"
        )
        assert rows[0]["url"] == "https://a.example"
        assert rows[0]["href"] == "https://a.example", "href 要同时映射到 url 字段供下游使用"
        assert rows[0]["snippet"] == "sa"

    def test_parses_json_array_too(self):
        """有些实现会把整个列表塞进一个块，两种都要能解析。"""
        result = _Result([json.dumps([
            {"title": "A", "href": "https://a.example", "body": "sa"},
            {"title": "B", "href": "https://b.example", "body": "sb"},
        ])])

        rows = mcp_search._parse_tool_result(result, "q")

        assert [r["title"] for r in rows] == ["A", "B"]

    def test_returns_empty_for_error_text(self):
        """工具报错时返回的是说明文本，不是结果。"""
        result = _Result(["Error executing tool search_text"])

        assert mcp_search._parse_tool_result(result, "q") == []

    def test_accepts_url_field_as_fallback(self):
        result = _Result([json.dumps({"title": "A", "url": "https://a.example"})])

        rows = mcp_search._parse_tool_result(result, "q")

        assert rows[0]["url"] == "https://a.example"

    def test_skips_non_dict_items(self):
        result = _Result([json.dumps(["not-a-dict", {"title": "A", "href": "u"}])])

        rows = mcp_search._parse_tool_result(result, "q")

        assert [r["title"] for r in rows] == ["A"]

    def test_handles_empty_result(self):
        assert mcp_search._parse_tool_result(_Result([]), "q") == []


class TestServerConfig:
    def test_loads_ddgs_entry_from_repo_config(self):
        config = mcp_search.load_mcp_server_config()

        assert config is not None, "仓库根目录应有 mcp_servers.json 且含 ddgs 条目"
        assert config.get("command"), "必须声明启动命令"

    def test_unknown_server_returns_none(self):
        assert mcp_search.load_mcp_server_config("no-such-server") is None

    def test_relative_uv_cache_dir_is_resolved_against_repo_root(self):
        """写用户级 uv 缓存可能没权限（实测 os error 5），配置里允许相对路径。"""
        env = mcp_search._build_env({"env": {"UV_CACHE_DIR": ".uv-cache"}})

        assert Path(env["UV_CACHE_DIR"]).is_absolute()
        assert Path(env["UV_CACHE_DIR"]).name == ".uv-cache"

    def test_absolute_env_value_is_kept(self):
        env = mcp_search._build_env({"env": {"DDGS_PROXY": "http://127.0.0.1:7897"}})

        assert env["DDGS_PROXY"] == "http://127.0.0.1:7897"


class TestProviderAvailability:
    def test_available_reflects_config(self):
        assert mcp_search.DdgsMcpProvider({"command": "uvx"}).available is True
        assert mcp_search.DdgsMcpProvider({}).available is False

    async def test_search_degrades_to_empty_when_unavailable(self):
        """与链上其他 Provider 一致：单源不可用只返回空列表，不抛异常。"""
        provider = mcp_search.DdgsMcpProvider({})

        assert await provider.search("q") == []
