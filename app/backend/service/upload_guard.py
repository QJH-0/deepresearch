"""上传内容真实性校验：扩展名可以改，文件头改不了。

扩展名白名单只挡住「改后缀」这种低级伪装。放行一个伪装成 .pdf 的二进制，
轻则解析器抛错，重则把不可信内容一路喂进切块与 LLM。因此白名单之后
必须再按内容签名验一次真实类型。
"""

import io
import logging
import zipfile

logger = logging.getLogger("backend.upload_guard")

# 签名判定只看文件头：整文件扫描对大文件是纯浪费
_SNIFF_BYTES = 4096

_PDF_MAGIC = b"%PDF-"
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC = b"PK\x03\x04"
_UTF8_BOM = b"\xef\xbb\xbf"

# 扩展名 → 允许的内容类型
EXPECTED_KINDS = {
    ".pdf": {"pdf"},
    ".doc": {"ole2"},
    ".docx": {"zip"},
    ".md": {"text"},
    ".markdown": {"text"},
    ".txt": {"text"},
    ".html": {"text"},
    ".htm": {"text"},
    ".csv": {"text"},
    ".json": {"text"},
}

_KIND_LABELS = {
    "pdf": "PDF",
    "ole2": "OLE2 复合文档",
    "zip": "ZIP/OOXML",
    "text": "文本",
    "binary": "未知二进制",
}


def sniff_kind(head: bytes) -> str:
    """按文件头判定真实类型：pdf / ole2 / zip / text / binary。"""
    if head.startswith(_PDF_MAGIC):
        return "pdf"
    if head.startswith(_OLE2_MAGIC):
        return "ole2"
    if head.startswith(_ZIP_MAGIC):
        return "zip"
    return "text" if _looks_like_text(head) else "binary"


def _looks_like_text(head: bytes) -> bool:
    sample = head[len(_UTF8_BOM):] if head.startswith(_UTF8_BOM) else head
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _docx_has_word_document(content: bytes) -> bool:
    """docx 必须是含 word/document.xml 的 OOXML 包，普通 ZIP 不算 Word 文档。"""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            return "word/document.xml" in zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


def check_upload(filename: str, content: bytes) -> str | None:
    """校验文件真实类型。通过返回 None，不通过返回可直接作 400 detail 的原因。"""
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    expected = EXPECTED_KINDS.get(ext)
    if expected is None:
        return f"不支持的文件格式: {ext or '(无扩展名)'}"

    kind = sniff_kind(content[:_SNIFF_BYTES])
    if kind not in expected:
        expected_label = "/".join(_KIND_LABELS[k] for k in sorted(expected))
        return f"文件内容与扩展名不符：{ext} 应为{expected_label}，实际为{_KIND_LABELS[kind]}"

    if ext == ".docx" and not _docx_has_word_document(content):
        return "docx 内容不是有效的 Word 文档（缺少 word/document.xml）"
    return None
