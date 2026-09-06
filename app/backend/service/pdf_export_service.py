"""R3.3: Playwright headless Chromium 报告 PDF 导出。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from functools import lru_cache

import markupsafe
import markdown as md_lib

logger = logging.getLogger("backend.service.pdf_export")

REPORT_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: "Noto Sans SC", "Microsoft YaHei", "PingFang SC", sans-serif;
         font-size: 11pt; line-height: 1.7; color: #1f2328; margin: 0; padding: 0; }}
  h1 {{ font-size: 18pt; border-bottom: 2px solid #d0d7de; padding-bottom: 8px; }}
  h2 {{ font-size: 14pt; margin-top: 1.4em; }}
  h3 {{ font-size: 12pt; }}
  code, pre {{ font-family: "JetBrains Mono", Consolas, "Courier New", monospace; }}
  pre {{ background: #f6f8fa; padding: 12px; border-radius: 6px; font-size: 9.5pt;
        white-space: pre-wrap; word-break: break-all; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1em 0; }}
  th, td {{ border: 1px solid #d0d7de; padding: 6px 10px; font-size: 10pt; text-align: left; }}
  blockquote {{ border-left: 4px solid #d0d7de; margin: 1em 0; padding: 0.2em 1em; color: #57606a; }}
  .meta {{ color: #57606a; font-size: 9pt; margin-bottom: 2em; }}
</style>
</head>
<body>
  <h1>{title}</h1>
  <div class="meta">DeepResearch 研报 · 生成时间 {generated_at}</div>
  {body_html}
</body>
</html>"""


def render_report_html(report_md: str, title: str = "DeepResearch 研究报告") -> str:
    """Markdown 正文转 HTML 并套入报告模板。

    对原始 Markdown 中的 HTML 特殊字符做转义防注入，
    然后走 markdown 渲染（fenced_code 扩展会在代码块内做二次转义）。
    """
    escaped = str(markupsafe.escape(report_md))
    body_html = md_lib.markdown(escaped, extensions=["tables", "fenced_code"])
    return REPORT_HTML_TEMPLATE.format(
        title=title,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        body_html=body_html,
    )


class PdfExportService:
    """Playwright headless Chromium 报告导出。"""

    TIMEOUT_SECONDS = 30.0

    async def export(self, report_html: str) -> bytes:
        """渲染 HTML 为 PDF。任何异常向上抛出，由 router 层降级。"""
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(report_html, wait_until="load")
                pdf = await asyncio.wait_for(
                    page.pdf(
                        format="A4",
                        margin={
                            "top": "20mm",
                            "bottom": "20mm",
                            "left": "15mm",
                            "right": "15mm",
                        },
                        print_background=True,
                        display_header_footer=True,
                        header_template="<span></span>",
                        footer_template=(
                            '<div style="font-size:8px; width:100%; text-align:center; color:#888;">'
                            '第 <span class="pageNumber"></span> 页 / 共 <span class="totalPages"></span> 页'
                            "</div>"
                        ),
                    ),
                    timeout=self.TIMEOUT_SECONDS * 1000,
                )
                return pdf
            finally:
                await browser.close()


@lru_cache(maxsize=1)
def get_pdf_export_service() -> PdfExportService:
    """单例获取 PdfExportService。"""
    return PdfExportService()
