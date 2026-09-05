"""
Advanced RAG Core — DeepResearch 项目 RAG 核心模块

基于行业最佳实践的 RAG 切片检索增强实现：
1. 语义感知切片（Semantic-Aware Chunking）— Markdown 结构切分 + 层级路径 metadata
2. Parent-Child 分块策略 — 子块精确检索，父块上下文增强
3. 查询重写（Query Rewriting）— LLM 生成多查询变体提高召回率
4. 混合检索（Hybrid Retrieval）— 向量语义检索 + BM25 关键词检索
5. 多路召回 + 重排序（Multi-Route Recall + Reranker）
6. 上下文压缩与扩展（Contextual Compression & Expansion）
7. 结构化 Metadata — doc_id / section_path / chunk_idx
"""

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from langchain_community.embeddings import DashScopeEmbeddings
from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
from langchain_openai import ChatOpenAI
from pymilvus import connections, utility

logger = logging.getLogger(__name__)

# 使用 langchain-milvus 新包
try:
    from langchain_milvus import Milvus as _MilvusVectorStore
    _MILVUS_BACKEND = "langchain_milvus"
except ImportError:
    from langchain_community.vectorstores import Milvus as _MilvusVectorStore
    _MILVUS_BACKEND = "langchain_community"


# ==============================================================================
# 配置
# ==============================================================================
@dataclass(frozen=True)
class RAGConfig:
    milvus_host: str = "127.0.0.1"
    milvus_port: int = 19530
    collection_name: str = "mult_agent_knowledge"
    parent_collection_name: str = "mult_agent_knowledge_parent"
    embedding_model: str = "text-embedding-v3"

    # 子块参数（精确检索）
    chunk_size: int = 512
    chunk_overlap: int = 64

    # 父块参数（上下文增强）
    parent_chunk_size: int = 2048
    parent_chunk_overlap: int = 100

    # 检索参数
    recall_k: int = 20          # 多路召回数量
    final_top_k: int = 5        # 最终返回数量
    rerank_model: str = "qwen-plus"

    # 是否启用查询重写
    enable_query_rewrite: bool = True

    # 是否启用 BM25 混合检索
    enable_bm25: bool = True

    # 是否启用 LLM 重排序
    enable_reranker: bool = True

    # 是否启用 Parent-Child 上下文扩展
    enable_parent_child: bool = True

    # PostgreSQL 全文检索（BM25 替代方案）
    enable_fulltext: bool = True
    postgres_dsn: str = ""


# ==============================================================================
# BM25 关键词检索器
# ==============================================================================
class BM25Retriever:
    """简易 BM25 关键词检索，与向量检索互补。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self._documents: List[Document] = []
        self._doc_tokens: List[List[str]] = []
        self._avgdl: float = 0.0
        self._df: Dict[str, int] = {}
        self._k1 = k1
        self._b = b

    def add_documents(self, docs: List[Document]):
        for doc in docs:
            tokens = self._tokenize(doc.page_content)
            self._documents.append(doc)
            self._doc_tokens.append(tokens)
            for token in set(tokens):
                self._df[token] = self._df.get(token, 0) + 1
        total_len = sum(len(toks) for toks in self._doc_tokens)
        self._avgdl = total_len / max(len(self._doc_tokens), 1)

    def search(self, query: str, k: int = 10) -> List[Tuple[Document, float]]:
        if not self._documents:
            return []
        query_tokens = self._tokenize(query)
        scores = []
        N = len(self._documents)
        for idx, doc_tokens in enumerate(self._doc_tokens):
            score = self._bm25_score(query_tokens, doc_tokens, N)
            scores.append((self._documents[idx], score))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:k]

    def _bm25_score(self, query_tokens: List[str], doc_tokens: List[str], N: int) -> float:
        score = 0.0
        dl = len(doc_tokens)
        for token in query_tokens:
            if token not in self._df:
                continue
            df = self._df[token]
            idf = (N - df + 0.5) / (df + 0.5)
            tf = doc_tokens.count(token)
            numerator = tf * (self._k1 + 1)
            denominator = tf + self._k1 * (1 - self._b + self._b * dl / max(self._avgdl, 1))
            score += idf * numerator / max(denominator, 1e-8)
        return score

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        tokens = []
        tokens.extend(re.findall(r'[a-zA-Z0-9._-]+', text.lower()))
        cn_chars = re.findall(r'[\u4e00-\u9fff]', text)
        for i in range(len(cn_chars) - 1):
            tokens.append(cn_chars[i] + cn_chars[i + 1])
        if len(cn_chars) == 1:
            tokens.append(cn_chars[0])
        return tokens

    def clear(self):
        self._documents = []
        self._doc_tokens = []
        self._avgdl = 0.0
        self._df = {}


# ==============================================================================
# 查询重写器
# ==============================================================================
class QueryRewriter:
    """使用 LLM 重写用户查询，生成多个检索变体以提高召回率。"""

    REWRITE_PROMPT = """你是一个查询重写专家。请将以下用户查询重写为 3 个不同的检索变体，以提高向量检索的召回率。

要求：
1. 保持原意不变
2. 使用不同的表述方式（同义词替换、句式变换、补充关键词）
3. 第 1 个变体为扩展版（补充相关术语），第 2 个为精简版（核心关键词），第 3 个为问题版（自然语言问句）
4. 每行一个变体，不要有编号

用户查询：{query}

输出格式（每行一个变体）："""

    def __init__(self, api_key: str, model: str = "qwen-plus"):
        self.llm = ChatOpenAI(
            api_key=api_key,
            model=model,
            base_url=os.getenv("BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            temperature=0.3,
            max_tokens=256,
        )

    def rewrite(self, query: str) -> List[str]:
        try:
            prompt = self.REWRITE_PROMPT.format(query=query)
            response = self.llm.invoke(prompt)
            variants = [line.strip() for line in response.content.strip().split("\n") if line.strip()]
            all_queries = [query] + [v for v in variants if v != query]
            return all_queries[:4]
        except Exception as e:
            logger.warning("查询重写失败，使用原始查询: %s", e)
            return [query]


# ==============================================================================
# LLM Reranker
# ==============================================================================
class LLMReranker:
    """使用 LLM 对检索结果进行 Cross-Encoder 风格的精排。"""

    RERANK_PROMPT = """你是一个相关性排序专家。请根据用户问题，对以下检索到的文档片段按相关性从高到低排序。

用户问题：{query}

文档片段：
{documents}

请只返回排序后的编号，用逗号分隔（如：3,1,4,2,5）。不要输出其他内容。"""

    def __init__(self, api_key: str, model: str = "qwen-plus"):
        self.llm = ChatOpenAI(
            api_key=api_key,
            model=model,
            base_url=os.getenv("BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            temperature=0.0,
            max_tokens=128,
        )

    def rerank(self, query: str, documents: List[Document], top_k: int = 5) -> List[Document]:
        if not documents:
            return []
        if len(documents) <= top_k:
            return documents

        try:
            doc_text = "\n".join(
                f"{i+1}. {doc.page_content[:300]}" for i, doc in enumerate(documents)
            )
            prompt = self.RERANK_PROMPT.format(query=query, documents=doc_text)
            response = self.llm.invoke(prompt)
            order_str = response.content.strip()
            order = []
            for num_str in re.findall(r'\d+', order_str):
                idx = int(num_str) - 1
                if 0 <= idx < len(documents) and idx not in order:
                    order.append(idx)
            for i in range(len(documents)):
                if i not in order:
                    order.append(i)
            return [documents[i] for i in order[:top_k]]
        except Exception as e:
            logger.warning("LLM 重排序失败，使用原始顺序: %s", e)
            return documents[:top_k]


# ==============================================================================
# PDF 文档专用解析器
# ==============================================================================
class PDFParser:
    """
    PDF 文档专用解析器：支持多策略解析，保留页码 metadata。

    解析策略（按优先级 fallback）：
    1. PyMuPDF (fitz) — 速度快，文本质量高，支持表格
    2. PyPDFLoader (pypdf) — LangChain 内置，兼容性好
    3. 纯文本提取后 OCR fallback（可选）

    参考：
    - GitHub: langchain-ai/chat-langchain (ingest.py 多格式 Loader 策略)
    - GitHub: arnobt78/RAG-PDF-Chat (PDF 逐页解析 + metadata 保留)
    - PyMuPDF 文档: https://pymupdf.readthedocs.io/
    """

    def __init__(self, enable_ocr_fallback: bool = False):
        self._enable_ocr = enable_ocr_fallback

    def parse(self, file_path: Path) -> List[Document]:
        """解析 PDF 文件，返回按页切分的 Document 列表。"""
        # 策略 1: 尝试 PyMuPDF (fitz) — 速度和质量最优
        docs = self._parse_with_pymupdf(file_path)
        if docs and any(d.page_content.strip() for d in docs):
            logger.info("PDF 解析策略: PyMuPDF | 页数=%d | file=%s", len(docs), file_path.name)
            return docs

        # 策略 2: fallback 到 PyPDFLoader
        docs = self._parse_with_pypdf(file_path)
        if docs and any(d.page_content.strip() for d in docs):
            logger.info("PDF 解析策略: PyPDFLoader | 页数=%d | file=%s", len(docs), file_path.name)
            return docs

        # 策略 3: OCR fallback（扫描件）
        if self._enable_ocr:
            docs = self._parse_with_ocr(file_path)
            if docs:
                logger.info("PDF 解析策略: OCR | 页数=%d | file=%s", len(docs), file_path.name)
                return docs

        logger.warning("PDF 解析失败或内容为空: %s", file_path.name)
        return []

    def _parse_with_pymupdf(self, file_path: Path) -> List[Document]:
        """使用 PyMuPDF (fitz) 解析 PDF，保留页码和文本块结构。"""
        try:
            import fitz  # PyMuPDF
        except ImportError:
            logger.debug("PyMuPDF 未安装，跳过该策略")
            return []

        try:
            doc = fitz.open(str(file_path))
            documents: List[Document] = []
            for page_num in range(len(doc)):
                page = doc[page_num]
                # 提取文本，保留段落结构
                text = page.get_text("text")  # "text" 模式保留换行
                if not text.strip():
                    continue
                # 基础清洗
                text = self._clean_page_text(text)
                documents.append(Document(
                    page_content=text,
                    metadata={
                        "source": str(file_path),
                        "source_name": file_path.name,
                        "page": page_num + 1,
                        "total_pages": len(doc),
                        "file_type": "pdf",
                    }
                ))
            doc.close()
            return documents
        except Exception as e:
            logger.warning("PyMuPDF 解析失败: %s", e)
            return []

    def _parse_with_pypdf(self, file_path: Path) -> List[Document]:
        """使用 LangChain PyPDFLoader 解析 PDF。"""
        try:
            from langchain_community.document_loaders import PyPDFLoader
            loader = PyPDFLoader(str(file_path))
            documents = loader.load()
            # 补充 metadata
            for doc in documents:
                doc.metadata.setdefault("source", str(file_path))
                doc.metadata.setdefault("source_name", file_path.name)
                doc.metadata.setdefault("file_type", "pdf")
            return documents
        except Exception as e:
            logger.warning("PyPDFLoader 解析失败: %s", e)
            return []

    def _parse_with_ocr(self, file_path: Path) -> List[Document]:
        """OCR fallback：解析扫描件 PDF（需安装 pytesseract + pdf2image）。"""
        try:
            import pytesseract
            from pdf2image import convert_from_path
            from PIL import Image

            images = convert_from_path(str(file_path))
            documents: List[Document] = []
            for page_num, image in enumerate(images, 1):
                text = pytesseract.image_to_string(image, lang="chi_sim+eng")
                if not text.strip():
                    continue
                text = self._clean_page_text(text)
                documents.append(Document(
                    page_content=text,
                    metadata={
                        "source": str(file_path),
                        "source_name": file_path.name,
                        "page": page_num,
                        "total_pages": len(images),
                        "file_type": "pdf",
                        "parse_method": "ocr",
                    }
                ))
            return documents
        except ImportError:
            logger.warning("OCR 依赖未安装 (pytesseract + pdf2image)，跳过 OCR 策略")
            return []
        except Exception as e:
            logger.warning("OCR 解析失败: %s", e)
            return []

    @staticmethod
    def _clean_page_text(text: str) -> str:
        """清洗 PDF 单页文本：去除页眉页脚、多余空白。"""
        # 去除控制字符
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
        # 合并连续空行
        text = re.sub(r"\n{3,}", "\n\n", text)
        # 去除行尾空白
        lines = [line.rstrip() for line in text.splitlines()]
        return "\n".join(lines).strip()


# ==============================================================================
# 语义切片器
# ==============================================================================
def create_semantic_chunker(
    chunk_size: int = 512,
    chunk_overlap: int = 64,
) -> Tuple[MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter]:
    """创建语义感知切片器：先按 Markdown 标题切分，再递归细切。"""
    markdown_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[
            ("#", "h1"),
            ("##", "h2"),
            ("###", "h3"),
            ("####", "h4"),
        ]
    )
    child_splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        length_function=len,
        separators=["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""],
    )
    return markdown_splitter, child_splitter


def create_parent_splitter(
    parent_chunk_size: int = 2048,
    parent_chunk_overlap: int = 100,
) -> RecursiveCharacterTextSplitter:
    """创建父块切片器。"""
    return RecursiveCharacterTextSplitter(
        chunk_size=parent_chunk_size,
        chunk_overlap=parent_chunk_overlap,
        length_function=len,
        separators=["\n\n", "\n", "。", "！", "？", "；", " ", ""],
    )


# ==============================================================================
# 高级 RAG 系统
# ==============================================================================
class RAGSystem:
    """
    Advanced RAG System — 整合语义切片、Parent-Child、查询重写、
    混合检索、重排序、上下文扩展的完整 RAG 系统。
    """

    def __init__(self, api_key: str, config: Optional[RAGConfig] = None):
        self.config = config or RAGConfig()
        self.api_key = api_key
        self.embeddings = DashScopeEmbeddings(
            model=self.config.embedding_model,
            dashscope_api_key=self.api_key,
        )

        # 初始化切片器
        self.markdown_splitter, self.child_splitter = create_semantic_chunker(
            chunk_size=self.config.chunk_size,
            chunk_overlap=self.config.chunk_overlap,
        )
        self.parent_splitter = create_parent_splitter(
            parent_chunk_size=self.config.parent_chunk_size,
            parent_chunk_overlap=self.config.parent_chunk_overlap,
        )

        # 连接 Milvus
        self._connect_to_milvus()

        # 子块向量库（精确检索）
        self.vectorstore = _MilvusVectorStore(
            embedding_function=self.embeddings,
            collection_name=self.config.collection_name,
            connection_args={"uri": f"http://{self.config.milvus_host}:{self.config.milvus_port}"},
            auto_id=True,
        )

        # 父块向量库（上下文增强）
        self.parent_store = _MilvusVectorStore(
            embedding_function=self.embeddings,
            collection_name=self.config.parent_collection_name,
            connection_args={"uri": f"http://{self.config.milvus_host}:{self.config.milvus_port}"},
            auto_id=True,
        )

        # BM25 检索器
        self.bm25 = BM25Retriever()

        # 查询重写器（延迟初始化）
        self._query_rewriter: Optional[QueryRewriter] = None

        # 重排序器（延迟初始化）
        self._reranker: Optional[LLMReranker] = None

        # Parent-Child 映射缓存
        self._parent_map: Dict[str, Document] = {}
        self._all_chunks: List[Document] = []

        logger.info(
            "RAG backend=%s | child_collection=%s | parent_collection=%s",
            _MILVUS_BACKEND, self.config.collection_name, self.config.parent_collection_name
        )

    def _connect_to_milvus(self) -> None:
        try:
            connections.connect(
                alias="default",
                host=self.config.milvus_host,
                port=self.config.milvus_port,
            )
        except Exception as exc:
            logger.error("连接 Milvus 失败: %s", exc)
            raise RuntimeError(
                f"连接 Milvus 失败 (host={self.config.milvus_host}, port={self.config.milvus_port}): {exc}"
            ) from exc

    @property
    def query_rewriter(self) -> QueryRewriter:
        if self._query_rewriter is None:
            self._query_rewriter = QueryRewriter(self.api_key, self.config.rerank_model)
        return self._query_rewriter

    @property
    def reranker(self) -> LLMReranker:
        if self._reranker is None:
            self._reranker = LLMReranker(self.api_key, self.config.rerank_model)
        return self._reranker

    # ==================================================================
    # 入库
    # ==================================================================

    def ingest_text(self, text: str, source: str) -> int:
        """
        语义感知入库：按 Markdown 结构切分，生成子块和父块。

        流程：
        1. 按 Markdown 标题结构切分，保留层级路径
        2. 父块切分（2048 字符）-> 存入父块集合
        3. 子块切分（512 字符）-> 存入子块集合，记录 parent_id
        4. 同步建立 BM25 索引
        """
        parent_docs, child_docs = self._semantic_split(text, source)

        # 写入父块
        if parent_docs:
            self.parent_store.add_documents(parent_docs)
            for doc in parent_docs:
                pid = doc.metadata.get("parent_id", "")
                if pid:
                    self._parent_map[pid] = doc

        # 写入子块
        if child_docs:
            self.vectorstore.add_documents(child_docs)
            self.bm25.add_documents(child_docs)
            self._all_chunks.extend(child_docs)

        return len(child_docs)

    def ingest_pdf(self, file_path: Path) -> int:
        """
        PDF 专用入库流程：多策略解析 → 逐页清洗 → 语义切分 → 入库。

        与 ingest_text 的区别：
        - 保留页码 metadata（page, total_pages）
        - 每页独立解析后合并，避免跨页文本混乱
        - 支持 PyMuPDF / PyPDF / OCR 三级 fallback
        """
        pdf_parser = PDFParser(enable_ocr_fallback=False)
        documents = pdf_parser.parse(file_path)
        if not documents:
            logger.warning("PDF 解析无结果，跳过入库: %s", file_path)
            return 0

        # 合并各页文本，在每页之间插入分页标记
        page_texts = []
        for doc in documents:
            page_num = doc.metadata.get("page", 0)
            page_texts.append(f"\n<!-- page_break: {page_num} -->\n{doc.page_content}")
        full_text = "\n\n".join(page_texts)

        # 调用语义切分入库，source 使用文件路径
        return self.ingest_text(full_text, source=str(file_path))

    def ingest_paths(self, paths: Iterable[Path]) -> int:
        """入库多个文件，自动识别格式并用对应 Loader 解析。"""
        import importlib
        total = 0
        # 文件扩展名 → (模块路径, 类名) 映射
        loader_map = {
            ".pdf": ("langchain_community.document_loaders", "PyPDFLoader"),
            ".docx": ("langchain_community.document_loaders", "Docx2txtLoader"),
            ".doc": ("langchain_community.document_loaders", "UnstructuredWordDocumentLoader"),
            ".html": ("langchain_community.document_loaders", "UnstructuredHTMLLoader"),
            ".htm": ("langchain_community.document_loaders", "UnstructuredHTMLLoader"),
            ".csv": ("langchain_community.document_loaders", "CSVLoader"),
        }
        for path in paths:
            ext = path.suffix.lower()
            if ext in (".txt", ".md", ".markdown"):
                text = path.read_text(encoding="utf-8")
            elif ext == ".pdf":
                # PDF 专用解析流程：PyMuPDF → PyPDF → OCR fallback
                chunks = self.ingest_pdf(path)
                total += chunks
                logger.info("入库(PDF): %s -> %d chunks (累计)", path.name, total)
                continue
            elif ext == ".json":
                # JSON 文件直接读取为文本，避免 JSONLoader 需要 jq_schema 参数
                text = path.read_text(encoding="utf-8")
            elif ext in loader_map:
                module_path, class_name = loader_map[ext]
                module = importlib.import_module(module_path)
                loader_cls = getattr(module, class_name)
                if ext in (".txt", ".md", ".markdown"):
                    loader = loader_cls(str(path), encoding="utf-8")
                else:
                    loader = loader_cls(str(path))
                docs = loader.load()
                text = "\n\n".join(d.page_content for d in docs)
            else:
                logger.warning("跳过不支持的格式: %s", path.name)
                continue
            total += self.ingest_text(text, source=str(path))
            logger.info("入库: %s -> %d chunks (累计)", path.name, total)
        return total

    def _semantic_split(self, text: str, source: str) -> Tuple[List[Document], List[Document]]:
        """语义切分文本，返回 (父块列表, 子块列表)。"""
        source_name = Path(source).name if source else "unknown"

        # Step 1: 按 Markdown 标题切分
        try:
            md_chunks = self.markdown_splitter.split_text(text)
        except Exception:
            # 非 Markdown 文本，直接作为单个块
            md_chunks = [Document(page_content=text, metadata={})]

        parent_docs: List[Document] = []
        child_docs: List[Document] = []

        for md_chunk in md_chunks:
            # 构建层级路径
            section_path = " > ".join(
                v for v in [
                    md_chunk.metadata.get('h1'),
                    md_chunk.metadata.get('h2'),
                    md_chunk.metadata.get('h3'),
                    md_chunk.metadata.get('h4'),
                ] if v
            )

            # Step 2: 父块切分
            parent_chunks = self.parent_splitter.split_text(md_chunk.page_content)
            for p_idx, p_chunk in enumerate(parent_chunks):
                parent_id = hashlib.md5(f"{source_name}:{section_path}:{p_idx}".encode()).hexdigest()[:12]
                parent_doc = Document(
                    page_content=p_chunk,
                    metadata={
                        **md_chunk.metadata,
                        "source": source,
                        "source_name": source_name,
                        "section_path": section_path,
                        "parent_id": parent_id,
                        "chunk_type": "parent",
                        "doc_id": source,
                    }
                )
                parent_docs.append(parent_doc)

                # Step 3: 子块切分
                child_chunks = self.child_splitter.split_text(p_chunk)
                for c_idx, c_chunk in enumerate(child_chunks):
                    child_id = hashlib.md5(f"{parent_id}:{c_idx}".encode()).hexdigest()[:12]
                    child_doc = Document(
                        page_content=c_chunk,
                        metadata={
                            **md_chunk.metadata,
                            "source": source,
                            "source_name": source_name,
                            "section_path": section_path,
                            "parent_id": parent_id,
                            "child_id": child_id,
                            "chunk_type": "child",
                            "chunk_idx": c_idx,
                            "doc_id": source,
                        }
                    )
                    child_docs.append(child_doc)

        return parent_docs, child_docs

    # ==================================================================
    # 检索
    # ==================================================================

    def search(self, query: str, k: int = 3) -> str:
        """格式化检索结果为字符串。"""
        try:
            records = self.search_records(query, k=k)
            if not records:
                return "未找到相关信息。"
            lines: List[str] = ["检索到的相关信息："]
            for idx, record in enumerate(records, 1):
                lines.append(f"{idx}. {record['snippet']}")
                lines.append(f"   (来源: {record['doc_id']})")
                if record.get('section_path'):
                    lines.append(f"   (章节: {record['section_path']})")
            return "\n".join(lines)
        except Exception as exc:
            logger.error("检索失败: %s", exc)
            return f"检索过程中发生错误: {str(exc)}"

    def search_records(self, query: str, k: int = 5) -> List[Dict[str, Any]]:
        """
        高级检索流程：

        1. 查询重写 -> 生成多个检索变体
        2. 多路召回: 向量语义检索 + PgSQL 全文检索
        3. 去重 + 合并
        4. LLM Reranker 精排
        5. Parent-Child 上下文扩展
        6. 返回最终 Top-K
        """
        if not utility.has_collection(self.config.collection_name):
            return []

        # Step 1: 查询重写
        if self.config.enable_query_rewrite:
            query_variants = self.query_rewriter.rewrite(query)
        else:
            query_variants = [query]
        logger.info("查询重写: %s", query_variants)

        # Step 2: 多路向量召回
        all_vector_docs: List[Document] = []
        for q in query_variants:
            docs = self.vectorstore.similarity_search(q, k=self.config.recall_k)
            all_vector_docs.extend(docs)

        # Step 3: BM25 召回
        bm25_docs: List[Document] = []
        if self.config.enable_bm25 and self.bm25._documents:
            bm25_results = self.bm25.search(query, k=self.config.recall_k)
            bm25_docs = [doc for doc, _ in bm25_results]

        # Step 4: 合并去重
        seen_hashes = set()
        merged_docs: List[Document] = []
        for doc in all_vector_docs + bm25_docs:
            content_hash = hashlib.md5(doc.page_content.encode()).hexdigest()
            if content_hash not in seen_hashes:
                seen_hashes.add(content_hash)
                merged_docs.append(doc)

        logger.info("多路召回: 向量=%d BM25=%d 去重后=%d",
                     len(all_vector_docs), len(bm25_docs), len(merged_docs))

        # Step 5: LLM 重排序
        if self.config.enable_reranker and len(merged_docs) > k * 2:
            reranked_docs = self.reranker.rerank(query, merged_docs, top_k=k * 2)
        else:
            reranked_docs = merged_docs[:k * 2]

        # Step 6: Parent-Child 上下文扩展
        final_records: List[Dict[str, Any]] = []
        seen_parents = set()

        for doc in reranked_docs[:k]:
            parent_id = doc.metadata.get("parent_id", "")

            if self.config.enable_parent_child and parent_id and parent_id in self._parent_map:
                if parent_id not in seen_parents:
                    seen_parents.add(parent_id)
                    parent_doc = self._parent_map[parent_id]
                    source = str(parent_doc.metadata.get("source") or "").strip()
                    title = Path(source).name if source else f"本地知识片段-{len(final_records)+1}"
                    final_records.append({
                        "source_id": f"LOC-{len(final_records)+1}",
                        "doc_id": source,
                        "title": title,
                        "snippet": parent_doc.page_content,
                        "source_type": "local",
                        "metadata": parent_doc.metadata,
                        "section_path": parent_doc.metadata.get("section_path", ""),
                        "chunk_type": "parent",
                        "matched_child_preview": doc.page_content[:200] + "...",
                    })
            else:
                metadata = doc.metadata or {}
                source = str(metadata.get("source") or "").strip()
                title = Path(source).name if source else f"本地知识片段-{len(final_records)+1}"
                final_records.append({
                    "source_id": f"LOC-{len(final_records)+1}",
                    "doc_id": source,
                    "title": title,
                    "snippet": doc.page_content,
                    "source_type": "local",
                    "metadata": metadata,
                    "section_path": metadata.get("section_path", ""),
                    "chunk_type": "child",
                })

        return final_records[:k]

    # ==================================================================
    # 基础操作
    # ==================================================================

    def add_documents(self, documents: List[Document]) -> int:
        self.vectorstore.add_documents(documents)
        self.bm25.add_documents(documents)
        return len(documents)

    def search_simple(self, query: str, k: int = 5) -> List[Dict[str, Any]]:
        """简化的向量检索（跳过重写、重排序、Parent-Child），用于低延迟场景。"""
        if not utility.has_collection(self.config.collection_name):
            return []
        docs = self.vectorstore.similarity_search(query, k=k)
        records: List[Dict[str, Any]] = []
        for idx, doc in enumerate(docs, 1):
            metadata = doc.metadata or {}
            source = str(metadata.get("source") or "").strip()
            title = Path(source).name if source else f"本地知识片段-{idx}"
            records.append({
                "source_id": f"LOC-{idx}",
                "doc_id": source,
                "title": title,
                "snippet": doc.page_content,
                "source_type": "local",
                "metadata": metadata,
            })
        return records
