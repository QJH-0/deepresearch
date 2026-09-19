"""网页抓取工具测试（B2）。

重点覆盖 SSRF 防护与 HTML 正文抽取——前者是安全底线，
后者决定 agent 拿到的是正文还是满屏脚本。

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    python -m pytest app/test/test_fetch_url.py -v
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
sys.path.insert(0, str(_APP_PATH))

from mult_agents import tools as tools_module  # noqa: E402


def _fake_response(body: str, status: int = 200, content_type: str = "text/html"):
    response = MagicMock()
    response.status_code = status
    response.text = body
    response.headers = {"content-type": content_type}
    return response


class TestSsrfProtection:
    """只允许公网 http/https —— 域名白名单不够，必须看解析后的 IP。"""

    def test_rejects_non_http_scheme(self):
        result = tools_module.fetch_url("file:///etc/passwd")

        assert result.startswith("[抓取失败]")
        assert "http/https" in result

    def test_rejects_loopback(self):
        result = tools_module.fetch_url("http://127.0.0.1:8080/admin")

        assert result.startswith("[抓取失败]")
        assert "公网" in result

    def test_rejects_private_range(self):
        result = tools_module.fetch_url("http://10.0.0.1/internal")

        assert result.startswith("[抓取失败]")

    def test_rejects_link_local_metadata_endpoint(self):
        """云元数据地址 169.254.169.254 —— 最典型的 SSRF 目标。"""
        result = tools_module.fetch_url("http://169.254.169.254/latest/meta-data/")

        assert result.startswith("[抓取失败]")

    def test_rejects_missing_hostname(self):
        result = tools_module.fetch_url("http:///path")

        assert result.startswith("[抓取失败]")

    def test_does_not_call_network_for_rejected_url(self, monkeypatch):
        called = []
        monkeypatch.setattr(tools_module.httpx, "get", lambda *a, **kw: called.append(1))

        tools_module.fetch_url("http://127.0.0.1/")

        assert called == [], "被拒绝的 URL 不得发起任何网络请求"


class TestHtmlExtraction:
    def test_drops_script_style_and_nav(self):
        html = (
            "<html><head><style>body{color:red}</style>"
            "<script>alert('x')</script></head>"
            "<body><nav>导航栏</nav><p>正文第一段</p>"
            "<footer>版权信息</footer></body></html>"
        )
        extractor = tools_module._HtmlTextExtractor()
        extractor.feed(html)

        text = extractor.text

        assert "正文第一段" in text
        for noise in ("alert", "color:red", "导航栏", "版权信息"):
            assert noise not in text, f"{noise} 应被丢弃，否则会污染证据"

    def test_keeps_block_structure(self):
        extractor = tools_module._HtmlTextExtractor()
        extractor.feed("<p>第一段</p><p>第二段</p>")

        assert "第一段" in extractor.text
        assert "第二段" in extractor.text
        assert "\n" in extractor.text, "块级标签应产生换行，否则正文会糊成一行"


class TestFetchUrlBehaviour:
    def test_extracts_text_from_html_response(self, monkeypatch):
        monkeypatch.setattr(
            tools_module.httpx, "get",
            lambda *a, **kw: _fake_response("<html><body><p>抓到的正文</p></body></html>"),
        )

        result = tools_module.fetch_url("http://93.184.216.34/page")

        assert "抓到的正文" in result
        assert "<p>" not in result

    def test_truncates_long_body(self, monkeypatch):
        monkeypatch.setattr(
            tools_module.httpx, "get",
            lambda *a, **kw: _fake_response("字" * 5000, content_type="text/plain"),
        )

        result = tools_module.fetch_url("http://93.184.216.34/big", max_chars=100)

        assert len(result) < 500
        assert "已截断" in result

    def test_http_error_returns_message_not_exception(self, monkeypatch):
        """工具输出直接进 agent 上下文，失败原因本身就是它决定下一步的依据。"""
        monkeypatch.setattr(tools_module.httpx, "get", lambda *a, **kw: _fake_response("", status=404))

        result = tools_module.fetch_url("http://93.184.216.34/missing")

        assert result.startswith("[抓取失败]")
        assert "404" in result

    def test_request_exception_returns_message(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(tools_module.httpx, "get", boom)

        result = tools_module.fetch_url("http://93.184.216.34/x")

        assert result.startswith("[抓取失败]")
        assert "connection reset" in result

    def test_empty_body_returns_message(self, monkeypatch):
        monkeypatch.setattr(tools_module.httpx, "get", lambda *a, **kw: _fake_response("   "))

        result = tools_module.fetch_url("http://93.184.216.34/empty")

        assert result.startswith("[抓取失败]")


class TestFetchUrlTool:
    """工具描述即 ACI：Anthropic 说它和 prompt 同等重要，值得被断言守住。"""

    def test_tool_has_name_and_usage_guidance(self):
        description = tools_module.fetch_url_tool.description

        assert tools_module.fetch_url_tool.name == "fetch_url_tool"
        assert "何时使用" in description, "工具描述必须写清使用时机，否则 agent 会乱调"
        assert "不要" in description, "必须给出不该用的边界，否则会被滥用"
