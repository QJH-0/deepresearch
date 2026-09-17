"""报告导出前质量检查与导出后回读校验。

门禁分两级：error 阻断导出，warning 提醒核对但放行。本文件覆盖两条链路
各自的判定，以及「正常报告必须放行」这条底线 —— 门禁最怕的是误伤。

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    set PYTHONPATH=app
    python -m pytest app/test/test_report_check.py -v
"""

import io
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "app"))

from backend.service.report_check import ReportIssue, check_report  # noqa: E402


def _report(body: str, references: str = "") -> str:
    return f"# 测试报告\n\n{body}\n\n{references}" if references else f"# 测试报告\n\n{body}"


_LONG_BODY = "本报告分析了市场格局与主要参与者，并对未来趋势作出判断。" * 40
_REFS = "## 参考资料\n\n- [WEB1_1-1] 某来源 https://example.com/a\n"


class TestCheckReportErrors:
    """结构性问题必须阻断导出。"""

    def test_empty_report_blocked(self):
        result = check_report("")

        assert not result.export_allowed
        assert [item.code for item in result.errors] == ["empty_report"]

    def test_citations_without_reference_section_blocked(self):
        result = check_report(_report(f"{_LONG_BODY} 结论有据 [WEB1_1-1]。"))

        assert not result.export_allowed
        assert "missing_reference_section" in [item.code for item in result.errors]

    def test_empty_reference_section_blocked(self):
        result = check_report(_report(f"{_LONG_BODY} 结论有据 [WEB1_1-1]。", "## 参考资料\n"))

        assert not result.export_allowed
        assert "empty_reference_section" in [item.code for item in result.errors]


class TestCheckReportPasses:
    """正常报告必须放行 —— 门禁最怕误伤。"""

    def test_well_formed_report_allowed(self):
        result = check_report(_report(f"{_LONG_BODY} 结论有据 [WEB1_1-1]。", _REFS))

        assert result.export_allowed
        assert result.errors == ()

    def test_local_source_dedup_does_not_trigger_error(self):
        """参考清单按 locator 去重，正文引用与清单编号不完全对应是正常的。"""
        body = f"{_LONG_BODY} 本地资料显示 [LOC1_1-1] 与 [LOC1_1-3] 互相印证。"
        references = "## 参考资料\n\n- [LOC1_1-1] 内部手册\n"

        result = check_report(_report(body, references))

        assert result.export_allowed


class TestCheckReportWarnings:
    """观感问题只提示，不阻断。"""

    def test_short_body_warns_but_allows(self):
        result = check_report(_report("太短了。"))

        assert result.export_allowed
        assert "short_body" in [item.code for item in result.warnings]

    def test_no_citation_warns(self):
        result = check_report(_report(_LONG_BODY))

        assert result.export_allowed
        assert "no_citation" in [item.code for item in result.warnings]

    def test_single_source_dominant_warns(self):
        body = f"{_LONG_BODY} [WEB1_1-1] 甲 [WEB1_1-1] 乙 [WEB1_1-1] 丙 [WEB1_1-2] 丁"
        references = "## 参考资料\n\n- [WEB1_1-1] A\n- [WEB1_1-2] B\n"

        result = check_report(_report(body, references))

        assert result.export_allowed
        assert "single_source_dominant" in [item.code for item in result.warnings]

    def test_reference_listed_but_not_cited_warns(self):
        body = f"{_LONG_BODY} 只有一条引用 [WEB1_1-1]。"
        references = "## 参考资料\n\n- [WEB1_1-1] A\n- [WEB1_1-9] B\n"

        result = check_report(_report(body, references))

        assert result.export_allowed
        assert "reference_not_cited" in [item.code for item in result.warnings]

    def test_issues_are_deduped(self):
        result = check_report(_report(_LONG_BODY))

        codes = [item.code for item in result.issues]
        assert len(codes) == len(set(codes))


class TestVerifyPdf:
    """导出后回读：只判「文件是否成立」，排版观感不在这里管。"""

    def test_unreadable_bytes_is_error(self):
        from backend.service.pdf_export_service import verify_pdf

        result = verify_pdf(b"%PDF-1.4 mock", "# 标题\n\n正文")

        assert not result.export_allowed
        assert [item.code for item in result.errors] == ["pdf_unreadable"]

    def test_blank_pdf_has_no_error(self):
        from backend.service.pdf_export_service import verify_pdf

        result = verify_pdf(_blank_pdf(), "# 标题\n\n正文")

        assert result.export_allowed

    def test_missing_title_warns_but_allows(self):
        from backend.service.pdf_export_service import verify_pdf

        with patch("pypdf.PdfReader", return_value=_fake_reader("完全无关的文字")):
            result = verify_pdf(_blank_pdf(), "# 目标标题\n\n正文")

        assert result.export_allowed
        assert "pdf_title_missing" in [item.code for item in result.warnings]

    def test_present_title_no_warning(self):
        from backend.service.pdf_export_service import verify_pdf

        with patch("pypdf.PdfReader", return_value=_fake_reader("前 目标标题 后")):
            result = verify_pdf(_blank_pdf(), "# 目标标题\n\n正文")

        assert result.export_allowed
        assert result.warnings == ()


class TestReportIssueModel:
    def test_export_allowed_depends_only_on_errors(self):
        from backend.service.report_check import ReportCheckResult

        only_warning = ReportCheckResult((ReportIssue("warning", "w", "m"),))
        with_error = ReportCheckResult((ReportIssue("error", "e", "m"),))

        assert only_warning.export_allowed
        assert not with_error.export_allowed


def _blank_pdf() -> bytes:
    """用 pypdf 生成一份结构合法的空白 PDF（不依赖浏览器）。"""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _fake_reader(text: str):
    """构造 PdfReader 替身，用于验证文本比对逻辑而不依赖真实文字型 PDF。"""
    page = MagicMock()
    page.extract_text.return_value = text
    reader = MagicMock()
    reader.pages = [page]
    return reader
