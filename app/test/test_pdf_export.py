"""R3.3 PDF 导出管线 — 单元测试。

覆盖: 正常导出 PDF、Playwright 失败降级 Markdown、无报告 404、
      HTML 模板渲染、weasyprint 引用清零。

Playwright 调用全部 mock，不依赖真实浏览器。
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.responses import Response

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
sys.path.insert(0, str(_APP_PATH))


def _make_research_service_mock(messages=None):
    """构造 mock ResearchService，get_thread_messages 已 stub。"""
    svc = MagicMock()
    svc._ensure_initialized = MagicMock()
    if messages is None:
        messages = [
            {"role": "user", "content": "测试问题"},
            {"role": "assistant", "content": "# 测试报告\n\n这是一份测试报告。"},
        ]
    svc.get_thread_messages = AsyncMock(return_value=messages)
    return svc


def _make_mock_playwright(pdf_bytes=b"%PDF-1.4 mock"):
    """构造 mock async_playwright，page.pdf 返回假 PDF 字节。"""
    mock_page = MagicMock()
    mock_page.set_content = AsyncMock()
    mock_page.pdf = AsyncMock(return_value=pdf_bytes)

    mock_browser = MagicMock()
    mock_browser.new_page = AsyncMock(return_value=mock_page)
    mock_browser.close = AsyncMock()

    mock_chromium = MagicMock()
    mock_chromium.launch = AsyncMock(return_value=mock_browser)

    mock_playwright = MagicMock()
    mock_playwright.chromium = mock_chromium

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_playwright)
    mock_cm.__aexit__ = AsyncMock(return_value=None)

    return mock_cm


# ── T3.3-01 正常导出 PDF ───────────────────────────────────────

class TestExportPdfSuccess:
    """Playwright 正常生成 PDF。"""

    @pytest.mark.asyncio
    async def test_export_pdf_returns_pdf(self):
        from backend.router.research_router import export_pdf
        from backend.service.pdf_export_service import get_pdf_export_service

        svc = _make_research_service_mock()
        mock_cm = _make_mock_playwright(b"%PDF-1.4 fake-content")

        get_pdf_export_service.cache_clear()

        with patch("playwright.async_api.async_playwright", return_value=mock_cm):
            response = await export_pdf("test-thread-abc123", svc)

        assert isinstance(response, Response)
        assert response.media_type == "application/pdf"
        assert b"%PDF" in response.body
        cd = response.headers.get("content-disposition", "")
        assert "report_" in cd
        assert ".pdf" in cd

        mock_page = mock_cm.__aenter__.return_value.chromium.launch.return_value.new_page.return_value
        call_kwargs = mock_page.pdf.call_args.kwargs
        assert call_kwargs.get("format") == "A4"
        assert call_kwargs.get("display_header_footer") is True
        assert "pageNumber" in call_kwargs.get("footer_template", "")
        assert "totalPages" in call_kwargs.get("footer_template", "")

        get_pdf_export_service.cache_clear()


# ── T3.3-02 Playwright 失败降级 Markdown ──────────────────────────

class TestExportPdfFallbackMarkdown:
    """Playwright launch 失败时降级返回 Markdown。"""

    @pytest.mark.asyncio
    async def test_export_pdf_fallback_to_markdown(self):
        from backend.router.research_router import export_pdf
        from backend.service.pdf_export_service import get_pdf_export_service

        report_md = "# 降级测试\n\n报告内容。"
        svc = _make_research_service_mock(messages=[
            {"role": "assistant", "content": report_md},
        ])

        get_pdf_export_service.cache_clear()

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(side_effect=RuntimeError("browser launch failed"))
        mock_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("playwright.async_api.async_playwright", return_value=mock_cm):
            response = await export_pdf("fallback-thread", svc)

        assert isinstance(response, Response)
        assert response.media_type == "text/markdown; charset=utf-8"
        assert response.body.decode("utf-8") == report_md
        cd = response.headers.get("content-disposition", "")
        assert ".md" in cd

        get_pdf_export_service.cache_clear()


# ── T3.3-03 无报告 404 ───────────────────────────────────────

class TestExportPdfNotFound:
    """无报告内容时返回 404。"""

    @pytest.mark.asyncio
    async def test_no_messages_404(self):
        from backend.router.research_router import export_pdf

        svc = _make_research_service_mock(messages=[])
        with pytest.raises(HTTPException) as exc_info:
            await export_pdf("empty-thread", svc)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_no_assistant_message_404(self):
        from backend.router.research_router import export_pdf

        svc = _make_research_service_mock(messages=[
            {"role": "user", "content": "只有用户消息"},
        ])
        with pytest.raises(HTTPException) as exc_info:
            await export_pdf("no-report-thread", svc)
        assert exc_info.value.status_code == 404


# ── T3.3-04 HTML 模板渲染 ──────────────────────────────────────

class TestRenderReportHtml:
    """验证 HTML 模板渲染：标题→h1、代码→pre/code、表格→table、转义。"""

    def test_template_renders_headings_code_tables(self):
        from backend.service.pdf_export_service import render_report_html

        md_text = "# 标题\n\n段落文本\n\n```python\nprint('hello')\n```\n\n| 列1 | 列2 |\n|---|---|\n| A | B |"
        html = render_report_html(md_text)

        assert "<h1>" in html
        assert "<pre>" in html
        assert "<code" in html
        assert "<table>" in html
        assert "<th>" in html
        assert "Noto Sans SC" in html or "Microsoft YaHei" in html

    def test_template_escapes_raw_html(self):
        from backend.service.pdf_export_service import render_report_html

        md_with_html = "# 标题\n\n<script>alert('xss')</script>"
        html = render_report_html(md_with_html)

        assert "<script>" not in html
        assert "&lt;script&gt;" in html


# ── T3.3-05 weasyprint 引用清零 ────────────────────────────────

class TestWeasyprintRemoved:
    """app/ 和 requirements.txt 中不再出现 weasyprint（文档注释除外）。"""

    def test_no_weasyprint_in_app_code(self):
        """app/ 源码文件（.py）中不含 weasyprint（排除测试文件的文档注释）。"""
        app_dir = _APP_PATH
        for py_file in app_dir.rglob("*.py"):
            content = py_file.read_text(encoding="utf-8", errors="ignore")
            # 排除测试文件中描述性提及 weasyprint
            if "test_" in py_file.name:
                # 测试文件中允许在注释/文档字符串中提及 weasyprint（用于描述替换）
                continue
            assert "weasyprint" not in content.lower(), f"weasyprint 出现在 {py_file}"

    def test_no_weasyprint_in_requirements(self):
        req_path = _PROJECT_ROOT / "requirements.txt"
        content = req_path.read_text(encoding="utf-8")
        assert "weasyprint" not in content.lower(), "requirements.txt 仍含 weasyprint"
