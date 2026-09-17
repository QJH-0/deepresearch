"""报告导出前的质量检查。

分两级：error 阻断导出，warning 提醒核对但放行。分级依据是「这份文件交出
去会不会出错」，而不是「写得够不够好」—— 后者是主观判断，不该由代码替用户决定。

为什么不做「引用编号悬空」的逐编号比对：参考资料清单会按 locator 对本地来源
去重（同一文件的多个 chunk 只列一个代表），因此正文引用 [LOC1_1-3] 而清单只列
[LOC1_1-1] 是正常现象，逐编号比对会在每份正常报告上误报。改为检查更可靠的
信号：有引用就必须有非空的参考资料段落。
"""

import re
from dataclasses import dataclass
from typing import Literal

Severity = Literal["error", "warning"]

# 与 nodes/_fallbacks.py 的引用格式保持一致
_CITATION_PATTERN = re.compile(r"\[([A-Z]+\d+_\d+-\d+)\]")
_REFERENCE_HEADINGS = ("## 参考资料", "## 引用列表", "## 来源清单")
_HEADING_PATTERN = re.compile(r"^#{1,3}\s+(.+)$", re.MULTILINE)

# 研报正文预期 2000 字以上，低于此值基本可以确定是生成中断或内容被截断
_MIN_BODY_CHARS = 800
# 单一来源引用占比上限：超过则报告结论实际由一家之言支撑
_MAX_SINGLE_SOURCE_SHARE = 0.6


@dataclass(frozen=True)
class ReportIssue:
    """一条报告问题。severity=error 阻断导出，warning 仅提示。"""

    severity: Severity
    code: str
    message: str


@dataclass(frozen=True)
class ReportCheckResult:
    """检查结果。errors 非空即不允许导出。"""

    issues: tuple[ReportIssue, ...] = ()

    @property
    def errors(self) -> tuple[ReportIssue, ...]:
        return tuple(item for item in self.issues if item.severity == "error")

    @property
    def warnings(self) -> tuple[ReportIssue, ...]:
        return tuple(item for item in self.issues if item.severity == "warning")

    @property
    def export_allowed(self) -> bool:
        return not self.errors

    def as_dicts(self) -> list[dict]:
        return [
            {"severity": item.severity, "code": item.code, "message": item.message}
            for item in self.issues
        ]


def _split_body_and_references(content: str) -> tuple[str, str | None]:
    """按第一个参考资料标题切分正文与参考清单；无参考段落时返回 None。"""
    for heading in _REFERENCE_HEADINGS:
        index = content.find(heading)
        if index != -1:
            return content[:index], content[index:]
    return content, None


def _extract_citations(text: str) -> list[str]:
    return list(dict.fromkeys(_CITATION_PATTERN.findall(text)))


def _body_char_count(body: str) -> int:
    """统计正文字数：去掉标题行与空白，避免拿标题凑字数。"""
    without_headings = _HEADING_PATTERN.sub("", body)
    return len(re.sub(r"\s", "", without_headings))


def dedupe_issues(issues: list[ReportIssue]) -> tuple[ReportIssue, ...]:
    """按 severity + code + message 去重，避免同类问题刷屏。"""
    seen: set[tuple[str, str, str]] = set()
    result: list[ReportIssue] = []
    for item in issues:
        key = (item.severity, item.code, item.message)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return tuple(result)


def check_report(content: str) -> ReportCheckResult:
    """检查报告文本，返回去重后的问题列表。"""
    issues: list[ReportIssue] = []
    body, references = _split_body_and_references(content or "")
    body_chars = _body_char_count(body)

    if not body.strip():
        issues.append(ReportIssue("error", "empty_report", "报告正文为空，无法导出"))
        return ReportCheckResult(tuple(issues))

    body_citations = _extract_citations(body)

    if body_citations:
        if references is None:
            issues.append(
                ReportIssue(
                    "error",
                    "missing_reference_section",
                    "正文含引用标记但缺少参考资料段落，引用无法核对",
                )
            )
        elif not _extract_citations(references):
            issues.append(
                ReportIssue("error", "empty_reference_section", "参考资料段落为空，引用无法核对")
            )
        else:
            uncited = [
                sid for sid in _extract_citations(references) if sid not in body_citations
            ]
            if uncited:
                issues.append(
                    ReportIssue(
                        "warning",
                        "reference_not_cited",
                        f"参考资料列出了正文未引用的条目：{', '.join(uncited[:5])}",
                    )
                )
    else:
        issues.append(
            ReportIssue("warning", "no_citation", "正文没有任何引用标记，结论无法溯源")
        )

    if body_chars < _MIN_BODY_CHARS:
        issues.append(
            ReportIssue(
                "warning",
                "short_body",
                f"正文仅 {body_chars} 字，低于研报预期（{_MIN_BODY_CHARS} 字）",
            )
        )

    # 按引用出现总次数判断，而不是去重后的条数 —— 3 处引用都指向同一来源
    # 恰恰是最该提醒的情形
    occurrences = _CITATION_PATTERN.findall(body)
    if len(occurrences) >= 3:
        counts: dict[str, int] = {}
        for sid in occurrences:
            counts[sid] = counts.get(sid, 0) + 1
        top_id, top_count = max(counts.items(), key=lambda item: item[1])
        share = top_count / len(occurrences)
        if share > _MAX_SINGLE_SOURCE_SHARE:
            issues.append(
                ReportIssue(
                    "warning",
                    "single_source_dominant",
                    f"单一来源 {top_id} 占全部引用的 {share:.0%}，结论可能依赖一家之言",
                )
            )

    return ReportCheckResult(dedupe_issues(issues))
