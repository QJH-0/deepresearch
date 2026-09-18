"""引用角标的唯一实现。

角标格式为 `[WEB1_1-1]` / `[LOC2_3-1]`：`大写字母 + 数字 _ 数字 - 数字`。
来源 ID 由检索节点按 `{前缀}{轮次}_{查询序号}-{记录序号}` 生成。

**放在顶层而非 `nodes/` 下**，是为了让 `eval_metrics` 这类评测模块能复用，
而不触发 `eval_metrics → nodes → write → eval_metrics` 的循环导入。
此前该正则散落在 `nodes/_fallbacks.py`、`backend/service/report_check.py`
与 `nodes/write.py` 三处，改一处漏两处。
"""

import re

CITATION_ID_PATTERN = re.compile(r"\[([A-Z]+\d+_\d+-\d+)\]")


def extract_citation_ids(content: str) -> list[str]:
    """提取正文中的引用角标 ID，去重保序。"""
    return list(dict.fromkeys(CITATION_ID_PATTERN.findall(content or "")))


def validate_and_fix_citations(content: str, valid_source_ids: set[str]) -> tuple[str, list[str]]:
    """移除正文中的非法角标，返回 (修正后正文, 实际使用的合法角标列表)。

    非法角标意味着引用溯源断链（角标指向来源表里不存在的 ID），直接删除而非保留。
    """
    def replace_citation(match: re.Match) -> str:
        citation_id = match.group(1)
        return f"[{citation_id}]" if citation_id in valid_source_ids else ""

    fixed_content = CITATION_ID_PATTERN.sub(replace_citation, content or "")
    used_ids = [cid for cid in extract_citation_ids(fixed_content) if cid in valid_source_ids]
    return fixed_content, used_ids
