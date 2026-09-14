# DeepResearch 项目审查报告 · 第二篇（检索与证据链深审）

| 项 | 值 |
| --- | --- |
| 审查日期 | 2026-09-14 |
| 承接 | `2026-09-14-deepresearch-code-review.md`（第一篇，24 项） |
| 本次范围 | `app/mult_agents/rag/core.py`(1022)、`nodes/_evidence.py`(604)、`nodes/_fallbacks.py`(451)、`nodes/_parsing.py`(124)、各节点（intent / plan / web_search / local_rag / deep_dive / analyze / clarify）、`schemas/research.py`、前端 `ClarifyCard.vue` |
| 新增问题 | 22（P1 ×6、P2 ×7、P3 ×9） |
| 累计问题 | 46（P0 ×2、P1 ×10、P2 ×16、P3 ×18） |
| 总体结论 | **产品宣称的核心能力「混合检索 + 重排 + 证据评分 + HITL 澄清」中，有 4 条链路经代码证据确认是断的**；其中 HITL 澄清链路断裂会直接产生 422 或错误数据 |

---

## 一、必须先看的一张图：断掉的四条链路

```
① PG 关键词检索     rag/core.py:155 查询 content_tsv 列
                    → 该列在整仓不存在（DDL 只在 docs/refactor 里标"待确认"）
                    → 永久 UndefinedColumn → except 静默 → 降级 BM25
                    → BM25 是纯内存，进程重启即空 ⇒ 关键词召回彻底失效

② 证据 LLM 融合     _fallbacks.py:64  from mult_agents.runtime import get_research_service
                    → runtime.py 没有这个符号 ⇒ ImportError
                    → except Exception: return None ⇒ EvidenceScorer 永远为 None
                    → config.json 的 evidence_llm_fusion: true 完全不生效
                    （即便修好 import，下一行 service.bundle 属性也不存在 —— 双重失效）

③ 相关性过滤        _evidence.py:233 _filter_web_records  /  :257 _filter_local_records
                    → 全仓零调用点（死代码）
                    → relevance_score 从未写入
                    → _fallbacks.py:434 读到空 ⇒ max_relevance 恒为 0.0
                    → 证据数 ≤2 时无条件判定"证据显著不足，最高仅 0.00"

④ HITL 澄清         analyze.py:62 与 clarify.py:314/369/416 共用 kind="clarification"
                    → 两者载荷结构不兼容，而 resume 校验只按 kind 派发
                    → analyze 分支：422（缺 answers 字段）
                    → clarify 分支：答案被字符串化（节点按 list 消费，实际收到 dict）
```

这四条的共同特征是：**失败路径都有 `except` 兜底，所以测试全绿、日志只有 warning，功能静默降级**。项目自建门禁 `GATE_STATUS=PASSED` 与这些问题的共存，说明门禁覆盖的是「接口存在 / 契约不抛异常」，而非「行为正确」。

---

## 二、P1 — 严重（新增 6 项）

### P1-5 HITL 澄清链路断裂：`kind="clarification"` 被两个语义不同的中断点复用

**证据链**

| 环节 | 位置 | 事实 |
| --- | --- | --- |
| 中断点 A | `nodes/clarify.py:314 / 369 / 416` | 载荷 `{kind:"clarification", questions:[...], message:"..."}`；节点期望 resume 返回**列表**（`answers if isinstance(answers, list) else [str(answers)]`） |
| 中断点 B | `nodes/analyze.py:62` | 载荷 `{kind:"clarification", node:"analyze", missing_gaps:[...], analysis_summary:"...", message:"..."}`；节点期望 resume 返回**字典** `{"action": "auto_search"\|"user_supply"\|"skip"}` |
| 校验层 | `router/research_router.py:225-240` | `_validate_resume_payload` 只按 `kind` 派发，`"clarification"` 一律走 `ClarifyResumePayload` |
| Schema | `schemas/research.py:34-37` | `ClarifyResumePayload` 要求 `answers: list[str]`（必填，无默认值） |
| 前端 | `agent_front/src/components/chat/ClarifyCard.vue:submit()` | 提交 `{kind:'clarification', answers: [...]}` |
| 默认开启 | `config.json:30` | `"analyze_clarify": true` |

**两个后果（都已确认）**

1. **analyze 分支无法 resume**：前端/调用方为 analyze 中断提交 `{"kind":"clarification","action":"skip"}` → `ClarifyResumePayload` 缺必填 `answers` → `ValidationError` → `ValueError` → **HTTP 422**。用户走不到 `analyze_node` 里读 `user_feedback.get("action")` 的那段逻辑。
2. **clarify 分支答案被破坏**：`ClarifyResumePayload` 校验通过后，`Command(resume={"kind":"clarification","answers":["A","B"]})` 使 `interrupt()` 返回**这个 dict**。而 `clarify.py:320` 判断 `isinstance(answers, list)` 为假 → 记录为 `{"q": [...], "a": ["{'kind': 'clarification', 'answers': ['A', 'B']}"]}` —— 用户答案被塞进一个字符串化的 dict 里。后续 `_llm_answer_sufficiency` 拿到的是这个结构。

**根因**：`kind` 被当作「中断类型的唯一标识」使用，但它实际标识的是「协议族」，而 `clarify` 与 `analyze` 是两个不同协议族却共用了同一个值。

**建议**
- 方案一（推荐）：为 analyze 引入独立 kind（如 `evidence_gap`），同步扩展 `InterruptRaisedData.kind` 的 `Literal`、`_validate_resume_payload`、前端新增对应卡片。改动面清晰，语义正确。
- 方案二：保留共用 kind，但把 `_validate_resume_payload` 改为「按 `kind` + 载荷特征」联合判别，并在节点内统一 `resume_value` 的取用方式。
- 无论哪种，都必须补一条**从 HTTP `/resume` 打到节点内部状态**的端到端断言 —— 现有 `test_p4` / `test_clarify_llm` 直接调用节点并传入裸 list，恰好绕过了 router 校验层，这是这个 bug 能存活的原因。

---

### P1-6 相关性过滤是死代码，级联导致「证据不足」误判

**证据**

- `_evidence.py:233 _filter_web_records`、`:257 _filter_local_records` —— 全仓 grep 零调用点。`web_search_node` / `local_rag_node` 只调用了 `_dedupe_sources` 与 `_minimal_record_filter`（后者仅检查字段非空，不做相关性判断）。
- `relevance_score` 的**唯一两个写入点**就在这两个死函数里（`:247`、`:268`）。
- `_fallbacks.py:434-447`：

```python
scored = [item for item in all_evidence if isinstance(item, dict) and item.get("relevance_score") is not None]
if scored: avg/max_relevance = ...
else:      avg_relevance = max_relevance = 0.0      # ← 恒走这支
if total <= 2 and max_relevance < 0.5:
    return False, f"证据显著不足：召回 {total} 条证据，... 最高仅 {max_relevance:.2f}..."
```

**后果**：`max_relevance` 恒为 `0.0` → 只要 `web_evidence + local_evidence` 数量 ≤ 2，`write_node` 就无条件拒写报告，并输出「最高仅 0.00」这一**误导性理由**（证据可能完全相关）。而 `_filter_local_records` 里那段注释明确写着这是为修「无关文档被当可靠来源成文」而做的 `0.2 → 0.35` 阈值调整 —— 那个修复从未运行。

**建议**：要么把两个过滤器接回 `web_search_node` / `local_rag_node`（在 `_minimal_record_filter` 之后），要么删除死函数并让 `_check_evidence_sufficiency` 改用别的相关性来源。**当前状态下，「修好了」的注释和「没生效」的代码同时存在，是最容易误导后续维护的形态。**

---

### P1-7 证据评分 LLM 融合完全不生效（双重失效 + 静默吞异常）

**证据**：`_fallbacks.py:57-74`

```python
def _get_evidence_scorer(state: AgentState):
    fusion_enabled = os.getenv("EVIDENCE_LLM_FUSION", "true").lower() in ("true","1","yes")
    if not fusion_enabled: return None
    try:
        from mult_agents.runtime import get_research_service   # ← runtime.py 无此符号
        service = get_research_service()
        if service is None or not hasattr(service, "bundle"):  # ← ResearchService 也无 bundle
            return None
        ...
    except Exception:
        return None                                            # ← ImportError 被吞
```

**已核实的事实**：
- `mult_agents/runtime.py` 导出 `colorize / AgentBundle / build_agent / build_agents / init_checkpointer / close_checkpointer / get_checkpointer / build_checkpointer` —— **没有 `get_research_service`**。该函数实际位于 `backend.service.research_service`。
- `ResearchService` 的实例属性是 `_app / _base_config / _thread_repo / _lock / _initialized` —— **没有 `bundle`**。

**后果**：`EvidenceScorer` 恒为 `None`，`_fallback_audit` 永远走纯先验分支（`_score_evidence`）。`config.json` 的 `evidence_llm_fusion: true` 与 `evidence_prior_weight: 0.4` 都是空转配置。而 `except Exception: return None` 让 ImportError 连 warning 都不留。

**建议**：改从 `backend.service` 导入；确认 `ResearchService` 暴露 LLM 的正式方式（当前根本没有，需补一个公开访问器，例如 `ResearchService.get_llm()`），并至少把异常降到 `logger.warning`。

---

### P1-8 PG 关键词检索引用了不存在的数据库列

**证据**：`rag/core.py:151-160`

```sql
SELECT id, content, doc_id, parent_id, section_path
FROM document_chunks
WHERE content_tsv @@ plainto_tsquery('simple', %s)
  AND vector_status = 'indexed'
ORDER BY ts_rank(content_tsv, plainto_tsquery('simple', %s)) DESC
LIMIT %s
```

全仓 `content_tsv` 只出现在：本处查询、`docs/refactor/phase1_检索链路/R1.2_PG关键词检索与RRF融合.md`（DDL 草案）、`docs/refactor/执行日志.md:28`（明确写着「DDL 状态：⚠️ 待用户确认后执行…未确认前 PG 检索不可用时自动走 BM25 降级」）。`postgres_client.py` 的 `DDL_DOCUMENT_CHUNKS` 与 `scripts/init_pgvector.sql` **都没有这个列**。

**后果**：`psycopg2` 抛 `UndefinedColumn` → `except Exception` → `logger.warning` → 返回 `[]`。**PG 全文检索路从未产生过任何结果**，且每次检索都要白付一次建连 + 异常开销。

**叠加放大**：`RAGSystem` 单例在三个调用点被初始化，配置互不相同（见 P2-8）。按正常启动顺序（`app_main._init_infra` 最先）构造的 `RAGConfig` **没有传 `postgres_dsn`** → `_keyword_retriever` 根本没被创建。而 BM25 是纯内存索引，进程重启后为空。因此**关键词召回在重启后的服务上完全不存在**，只剩向量一路。

**建议**：二选一 —— ① 把 `content_tsv` 生成列 + GIN 索引的 DDL 正式落到 `postgres_client.py`（并补历史数据回填）；② 删除 PG 关键词检索器与相关配置。不要保留「代码假设列存在、DDL 停在待确认」的中间态。

---

### P1-9 RRF 融合用法错误：把多路召回当成单一排名列表

**证据**：`rag/core.py:908-928`

```python
all_vector_docs = []
for q in query_variants:                       # 最多 4 个查询变体
    docs = self.vectorstore.similarity_search(q, k=self.config.recall_k)   # 每路 20 条
    all_vector_docs.extend(docs)               # ← 4 路被拍平成一个列表

merged_docs = rrf_fuse([all_vector_docs, keyword_docs], k=self.config.rrf_k)
```

`rrf_fuse` 的语义是 `score(doc) = Σ_i 1/(k + rank_i(doc))`，其中 `i` 遍历**传入的每个列表**。这里传入 2 个列表：一个是「4 路向量结果拼接成的 80 条」，另一个是关键词结果。

**后果**：变体 1 的第 1 名与变体 4 的第 20 名处在同一个列表的 rank 1 和 rank 80，`1/(60+1)` 与 `1/(60+80)` 相差一倍以上。也就是说，**一个文档排在第几个变体里、以及该变体内排第几，被混成了同一个维度**。查询重写带来的「多视角召回」优势被结构性削弱 —— 后序变体天然吃亏，而它们本应是平等的独立召回源。

**建议**：改成每路一个列表：

```python
vector_runs = [self.vectorstore.similarity_search(q, k=recall_k) for q in query_variants]
merged_docs = rrf_fuse([*vector_runs, keyword_docs], k=self.config.rrf_k)
```

项目里已有 `test_rag_rrf.py`，但显然只测了 `rrf_fuse` 的纯函数行为，没有覆盖「多路召回是否被正确拆分成多个列表」这一调用契约。建议补一条断言：变体数 > 1 时，`rrf_fuse` 收到的列表个数应等于变体数 + 关键词路数。

---

### P1-10 同步阻塞调用位于 async 节点内，破坏 SSE 并发

**证据**

- `nodes/local_rag.py:39`（`async def local_rag_node` 内部）调用 `search_knowledge_base_records(...)` → `RAGSystem.search_records()`，其内部依次执行：
  - `QueryRewriter.rewrite()` → `ChatOpenAI.invoke()`（**同步 LLM 调用**）
  - `vectorstore.similarity_search()` × 最多 4 次（**同步网络 I/O**）
  - `PostgresKeywordRetriever.search()` → `psycopg2.connect()`（**同步建连，无连接池**，每次调用新建）
  - `_rerank()` → `DashScopeReranker.rerank()` → `httpx.post()`（**同步 HTTP**）或 `LLMReranker.rerank()` → 又一次**同步 LLM 调用**
- `nodes/web_search.py:44`（`async def web_search_node` 内部）调用 `web_search_records(...)` → `tools.py:366-371`：

```python
if in_loop:
    loop = _get_chain_loop()
    future = asyncio.run_coroutine_threadsafe(chain.search(...), loop)
    return future.result(timeout=60)      # ← 在事件循环线程上阻塞等待
```

**后果**：LangGraph 直接在事件循环上 await 异步节点函数。上述调用会把**整个事件循环线程阻塞住**（本地检索最坏情况 = 4 次向量检索 + 1 次 PG 建连 + 2 次 LLM + 1 次 rerank HTTP；网页检索每个查询最多阻塞 60s）。期间同进程内所有其他 SSE 流的 token 推送、心跳、取消响应全部停摆。

这直接冲击项目的核心卖点：README 把「token 级流式输出」与「TaskRegistry 并发控制 / 多会话并行」列为关键能力，而只要两个会话同时跑研究，后者的流就会长时间卡死。`sse_heartbeat_seconds` 心跳机制在这种情况下也救不了 —— 因为发出心跳的协程同样跑在被阻塞的循环上。

**建议**：把这些阻塞调用统一改为 `await asyncio.to_thread(...)`（本地检索可整段包装），或为 RAG 链路提供原生 async 版本。`web_search_records` 的 `future.result()` 应改为 `await asyncio.wrap_future(future)` 并在节点内 await。这是本轮审查中**对用户体验影响最直接**的一项。

---

## 三、P2 — 中等（新增 7 项）

| # | 问题 | 证据 | 影响 |
| --- | --- | --- | --- |
| P2-10 | **RAGSystem 单例被三个调用点用不同配置初始化，「谁先跑谁赢」** | `app_main.py:129-134` 用 `collection_name="mult_agent_knowledge"`、**不传 `postgres_dsn`**；`runtime.py:95-99`（`build_agents`）用 `config.milvus_collection`（config.json 里是 `mult_agent_memory`）、**不传 `postgres_dsn`**；`document_service.py:146-152` 用 `"mult_agent_knowledge"` + 传 `postgres_dsn`。而 `tools.init_rag_system` 只在 `_RAG_SYSTEM is None` 时构造 | 读写的 Milvus collection 名与「是否启用 PG 关键词路」取决于初始化顺序，且两处 collection 名不一致（`mult_agent_knowledge` vs `mult_agent_memory`）。若 `app_main._init_infra` 的 RAG 初始化失败（`rag_init_ok=False`），下一个调用点会用**另一个 collection** 建成单例，与 `DocumentService` 写入的 collection 不一致 → 检索不到已上传文档 |
| P2-11 | **`_is_official_domain` 的 `"gov" in value` 过宽** | `_evidence.py:456-458`：`value.endswith(".gov.cn") or ... or "gov" in value or "official" in value` | `govtech.com`、`mygovnews.cn`、任何含 `gov` 子串的域名都被判为官方。而 `_filter_web_records:248` 让这类域名**豁免相关性过滤**，`_score_evidence:467` 再给 **0.88** 高分。若该过滤器被重新接线（见 P1-6），这会立刻变成「营销站拿到官方可信度」的问题 |
| P2-12 | **本地知识库固定 0.92 高可信先验** | `_evidence.py:462-465`：`source_type == "local" → return 0.92, "企业内部知识库证据，默认高可信"` | 本地文档可能是用户随手上传的、过期的或与问题无关的。固定先验使 `0.4×0.92 + 0.6×llm` 的下限被抬高到 0.368，压低 LLM 评分的话语权。与 `evidence_prior_weight: 0.4` 的设计意图相冲突 |
| P2-13 | **`EvidenceScorer` 评分 prompt 里的「研究问题」恒为空** | `_evidence.py:542`：`query = batch[0].get("query", "")`。而进入评分的 `web_evidence`/`local_evidence` 记录字段为 `{source_id,title,url/doc_id,snippet,domain,source_type,reliability_hint,supports,notes}` —— **无 `query` 键** | 评分 prompt 变成「研究问题：\n\n证据列表：…」，LLM 完全不知道要评的是什么问题，「相关性」这一评分维度失效，只剩泛化的内容质量判断。修好 P1-7 之后这个问题才会显现（当前被 P1-7 掩盖） |
| P2-14 | **精排阈值导致小结果集常态跳过重排** | `rag/core.py:934`：`if self.config.enable_reranker and len(merged_docs) > k * 2` | `local_rag_node` 传 `limit=4` → `k=4` → 阈值 8。RRF 融合后结果通常远少于 8 条 → **精排被跳过**，`gte-rerank-v2` 这个配置项在多数本地检索中空转。网页检索同理。建议改为「只要候选数 > 最终返回数就精排」 |
| P2-15 | **`iteration` 被三个节点以冲突语义写入，`write` 的迭代上限守卫失效** | `plan.py:93/114/148` 一律写 `iteration: 0`（归零）；`analyze.py:119`（reflect）写 `iteration+1`；`write.py:151/167` 写 `iteration+1` 但随后 `goto="plan"`，**被 plan 的归零覆盖** | `write_node` 里 `if iteration >= max_iter: 直接采纳` 这道防死循环守卫，因 plan 每次都把 iteration 清零而难以触发。同时「已迭代几轮」这一语义在不同节点含义不同，任何基于 iteration 的统计/展示都不可靠 |
| P2-16 | **父块内容 = 子块内容（补充根因）** | 第一篇 P2-4 已记录 `chunk_consumer.py:158` 与 `document_service.py:535` 用子块 `content` 构造父块。本轮发现：`rag/core.py:_semantic_split:830` **写对了**（`page_content=p_chunk`） | 根因是**同一套父子切分逻辑被实现了两遍**：`core.py._semantic_split`（返回 `(parent_docs, child_docs)`）与 `document_service._semantic_split`（返回 `[(child, meta)]`）。文档入库主链路走的是抄错的那一份，而 `ingest_text` 走的是对的那一份。消除重复实现即可根治 |

---

## 四、P3 — 轻微 / 工程卫生（新增 9 项）

| # | 问题 | 证据 |
| --- | --- | --- |
| P3-10 | **`analysis_summary` 不是 AgentState 字段，写入被静默丢弃** | `intent.py:85`（`direct_answer_node`）返回 `"analysis_summary": content`，但 `state.py` 只有 `analysis` 没有 `analysis_summary`。已用 langgraph 1.0.3 实测：节点返回未知 key **不报错、直接丢弃**（探针结果 `RESULT: {'a': 1}`）→ 该写入是死代码，且掩盖了「本该写 `analysis`」的意图 |
| P3-11 | **`intent_node` 把路由 JSON 写进 `draft`** | `intent.py:43` `"draft": content`（content 是 `{"route":...}` 的 JSON 文本）。`draft` 语义上属于报告草稿，`_render_reference_list` 也读 `draft`。虽被后续 `write_node` 覆盖，但语义污染 |
| P3-12 | **硬编码 `"__end__"` 而非 `END` 常量** | `plan.py:129` `Command(goto="__end__")`。值恰好等于 langgraph 的 `END`，但绕过常量后版本升级改名即静默失效 |
| P3-13 | **`rag/core.py` 内不可达分支与重复 loader 映射** | `:788` 在 `elif ext in loader_map:` 分支内再判断 `if ext in (".txt",".md",".markdown")` —— 这三种扩展名已在 `:773` 提前 `continue`，永远为假。`loader_map`（`:763-770`）与 `document_service.LOADER_MAP` 是同一份映射的第二份拷贝 |
| P3-14 | **BM25 与 `_parent_map` 为纯内存，重启即空** | `BM25Retriever._documents` / `RAGSystem._parent_map` / `_all_chunks` 均无持久化，且无容量上限。与 P1-8 叠加后，重启后关键词召回完全失效；`_parent_map` 还会随入库文档数无限增长 |
| P3-15 | **裸 `except Exception` 吞掉 ImportError 等结构性错误** | `_fallbacks.py:73-74`、`_get_evidence_scorer` 整段。P1-7 就是这个模式的直接产物 —— 一个 import 拼写错误被降级成了「功能未启用」 |
| P3-16 | **`_rerank` 中的 `is not None` 判断恒真** | `rag/core.py:991` `if self.reranker is not None:` —— `reranker` 是 property，内部 `if self._reranker is None: self._reranker = LLMReranker(...)`，**永不返回 None**。该判断无意义，且掩盖了「LLM 重排是必走路径」这一事实 |
| P3-17 | **`RAGConfig.rerank_model` 被复用于查询重写** | `rag/core.py:63` `rerank_model: str = "qwen-plus"`；`:692` `QueryRewriter(self.api_key, self.config.rerank_model)`；`:698` `LLMReranker(self.api_key, self.config.rerank_model)`。字段名与用途不符，且 `config.json` 另有 `rerank_model_name: "gte-rerank-v2"`（对应 `DashScopeReranker`），两个「rerank model」概念容易混淆 |
| P3-18 | **`_parsing._invoke_json_agent` 在流式无输出时二次调用 LLM** | `_parsing.py:99-100`：`if not content: result = await agent.ainvoke(...)`。对已在 `astream` 上消耗过一次额度的场景，会重复计费与重复耗时；且 `metadata` 变量（`:87`）未使用 |

---

## 五、本轮验证方法说明

为避免把猜测写成结论，本轮对以下三点做了**可执行验证**而非静态推断：

| 结论 | 验证方式 | 结果 |
| --- | --- | --- |
| `content_tsv` 列不存在 | 全仓 grep（含 `.pyc`、`docs/`、`scripts/*.sql`） | 仅查询与 DDL 草案中出现，DDL 从未落地 ✅ |
| `get_research_service` 不在 `runtime.py` | `grep "^def \|^class "` 列出 runtime 全部顶层定义 | 确认不存在，实际位于 `backend.service` ✅ |
| LangGraph 对未知 state key 的行为 | 用 `langgraph 1.0.3` 构造最小 StateGraph 探针，节点返回 `{"a":1,"unknown_key":"x"}` | **静默丢弃，不抛异常**（`RESULT: {'a': 1}`）→ P3-10 定级为 P3 而非 P1 ✅ |

未执行：前端 vitest、浏览器 E2E、真实 Milvus/PG 联调。因此 P1-8 / P1-9 / P1-10 的**运行期表现**（实际召回质量下降幅度、并发卡顿的具体秒数）属推断，需实机回归确认。这三项建议优先在真实中间件环境下复现后再定修法。

---

## 六、与第一篇合并后的修复优先级

**上线前（阻断）**
1. P0-1 凭据轮换 + 移出仓库 + 清理 git 历史
2. P0-2 统一鉴权；`ADMIN_TOKEN` 改 fail-closed

**修复「宣称可用但实际断链」的四条链路（严重）**
3. **P1-5** HITL 澄清协议拆分（会直接产生 422 与脏数据）
4. **P1-7** EvidenceScorer 导入修复（配置项空转）
5. **P1-6** 相关性过滤接线或删除（导致「证据不足」误判）
6. **P1-8** `content_tsv` DDL 落地或删除该路（关键词召回失效）
7. **P1-1** 记忆命名空间统一（第一篇）
8. **P1-2** 文档删除的向量清理（第一篇）
9. **P1-10** 同步调用改异步（影响 SSE 并发体验）
10. **P1-9** RRF 多路融合修正（影响召回质量）

**技术债专项（建议一次「纯净原则」清理）**
11. 消除父子切分的重复实现（P2-16 根因）、RAG 单例配置分叉（P2-10）、死代码（P1-6 的两个过滤器、P3-13、`tools.py` 未接线工具）、`iteration` 语义统一（P2-15）、裸 `except` 收口（P3-15）

---

## 七、审查局限（更新）

- `app/backend/service/document_service.py` 的 `_semantic_split` 与 `rag/core.py` 的同名方法已确认是两份实现，但未逐行比对全部差异，仅确认了父块 `page_content` 一处不一致。
- 前端 4 个 vitest 用例（`test/` 目录）未执行 —— `package.json` 无 `test` 脚本（第一篇 P3-4）。因此「HITL 卡片在 analyze 中断下渲染错乱」这一推论未经前端验证。
- 未运行真实 Milvus / PostgreSQL，故 P1-8 / P1-9 / P1-10 的运行期量化影响未实测。
- `docs/` 下的历史规划文档（含 `docs/refactor/执行日志.md`）本次只作为「DDL 未执行的佐证」引用，未系统核对文档与实现的整体一致性。
