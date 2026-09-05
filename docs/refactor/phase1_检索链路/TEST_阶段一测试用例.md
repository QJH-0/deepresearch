# 阶段一测试用例（检索链路）

> 配套任务：R1.1 Web 搜索 Provider 链式降级 / R1.2 PG 关键词检索与 RRF 融合 / R1.3 qwen3-rerank 专用重排 / R1.4 证据评分 LLM 融合
> 优先级：P0 = 验收必过；P1 = 应尽量通过；P2 = 建议覆盖
> 通用规约：全部单测不得依赖真实中间件与真实 DashScope API Key，一律 mock（参考 `app/test/conftest.py` 现有 mock 模式）。

## 1. 测试文件规划

| 测试文件 | 覆盖任务 | 说明 |
| --- | --- | --- |
| `app/test/test_search_provider.py`（扩展） | R1.1 | 在现有文件追加 T1.1 组用例 |
| `app/test/test_rag_rrf.py`（新增） | R1.2 | RRF 融合 + PG 关键词检索 |
| `app/test/test_rag_rerank.py`（新增） | R1.3 | DashScopeReranker + 降级链 |
| `app/test/test_evidence_scorer.py`（新增） | R1.4 | 证据评分融合 |

## 2. T1.1 Web 搜索 Provider 链式降级（扩展 test_search_provider.py）

### T1.1-01 TavilyProvider 归一化输出【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | mock `httpx.AsyncClient.post` 返回 `{"results": [{"title": "t", "url": "https://a.com/x", "content": "c"}]}`，状态 200 |
| 步骤 | `TavilyProvider(api_key="k").search("q", max_results=3)` |
| 预期 | 返回 1 条记录：`source_type=="web"`、`domain=="a.com"`、`title=="t"`、`snippet=="c"`、`source_id==""`、`published_at==""` |

### T1.1-02 TavilyProvider 无 Key 自禁用【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | `TAVILY_API_KEY` 未设置（monkeypatch delenv） |
| 步骤 | `TavilyProvider().available`；`TavilyProvider().search("q")` |
| 预期 | `available is False`；`search` 直接返回 `[]` 且不发起任何 HTTP 请求 |

### T1.1-03 Tavily HTTP 异常返回空【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | mock post 抛 `httpx.ConnectError`；再测返回 500 一组 |
| 步骤 | `TavilyProvider(api_key="k").search("q")` |
| 预期 | 两组均返回 `[]`，不抛异常 |

### T1.1-04 链式降级顺序短路【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | Provider A（mock，`search` 返回 `[]`）、Provider B（mock，返回 1 条记录）、Provider C（mock，返回 1 条记录并记录调用标记） |
| 步骤 | `SearchProviderChain([A, B, C]).search("q")` |
| 预期 | 返回 B 的记录；C 的 `search` 未被调用（短路） |

### T1.1-05 链内 Provider 抛异常继续降级【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | Provider A（mock，`search` 抛 `RuntimeError`）、Provider B（mock，返回 1 条） |
| 步骤 | `SearchProviderChain([A, B]).search("q")` |
| 预期 | 返回 B 的记录，无异常抛出 |

### T1.1-06 全链失败返回空列表【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 三个 mock Provider 均返回 `[]` |
| 步骤 | `SearchProviderChain([A, B, C]).search("q")` |
| 预期 | 返回 `[]`，不抛异常 |

### T1.1-07 available=False 的 Provider 被链过滤【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | `TavilyProvider()`（无 Key）+ 一个正常 mock Provider |
| 步骤 | 构造 `SearchProviderChain([tavily, mock])`，检查内部 provider 数量并 search |
| 预期 | 链中仅剩 mock Provider；search 正常返回 |

### T1.1-08 配置驱动 Provider 顺序【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | `config.json` 临时写 `"search_providers": ["searxng", "ddgs"]`（monkeypatch 配置读取） |
| 步骤 | 重置全局链单例后 `_get_provider_chain()` |
| 预期 | 链内 Provider 顺序为 SearXNG 在前；非法名（如 `"bing"`）被剔除；全非法时回退默认 `["ddgs", "searxng"]` |

### T1.1-09 web_search_records 兼容行为回归【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | monkeypatch 链中 DDG mock 返回 2 条、SearXNG mock 返回 1 条 |
| 步骤 | `web_search_records("q", count=5)` |
| 预期 | 返回 DDG 的 2 条（DDG 成功时不再调 SearXNG）；DDG 返回空时返回 SearXNG 的 1 条 |

### T1.1-10 SearXNGProvider 异步包装【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | `SEARX_URL` 设置；mock urllib 请求返回 SearXNG JSON 结构 `{"results": [{"title", "url", "content"}]}` |
| 步骤 | `await SearXNGProvider(url).search("q")`（asyncio 测试） |
| 预期 | 返回归一化记录；urllib 抛异常时返回 `[]` |

## 3. T1.2 RRF 融合与 PG 关键词检索（新增 test_rag_rrf.py）

### T1.2-01 RRF 基本融合排名【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 构造 Document 列表 A=`[d1, d2, d3]`，B=`[d2, d1, d4]`（d1~d4 内容互不相同） |
| 步骤 | `rrf_fuse([A, B], k=60)` |
| 预期 | d1（rank1+rank2）与 d2（rank2+rank1）总分相等且排最前；d3、d4 次之；结果无重复元素 |

### T1.2-02 RRF 单路输入【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | 只传一路 `[d1, d2]` |
| 步骤 | `rrf_fuse([[d1, d2]])` |
| 预期 | 顺序保持 `[d1, d2]`（等价于原序） |

### T1.2-03 RRF 同文档跨路合并【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | A 与 B 中各含一个 `page_content` 完全相同的 Document（不同对象实例） |
| 步骤 | `rrf_fuse([A, B])` |
| 预期 | 输出中该内容仅出现一次，分数为两路之和 |

### T1.2-04 RRF top_k 截断与空输入【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | 5 个文档两路融合 |
| 步骤 | `rrf_fuse([A, B], top_k=3)`；`rrf_fuse([])` |
| 预期 | 结果恰 3 条；空输入返回 `[]` |

### T1.2-05 PostgresKeywordRetriever 正常检索【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | mock `psycopg2.connect` 返回游标，fetchall 返回 `[("cid1", "content1", "doc1", "pid1", "sec")]` |
| 步骤 | `PostgresKeywordRetriever("dsn").search("q", k=5)` |
| 预期 | 返回 1 个 Document：`metadata` 含 `parent_id=="pid1"`、`doc_id=="doc1"`、`section_path=="sec"`、`chunk_id=="cid1"`；SQL 语句包含 `plainto_tsquery` 与 `ts_rank` 排序 |

### T1.2-06 PostgresKeywordRetriever 连接失败降级【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | mock `psycopg2.connect` 抛 `psycopg2.OperationalError` |
| 步骤 | `PostgresKeywordRetriever("dsn").search("q")` |
| 预期 | 返回 `[]`，不抛异常 |

### T1.2-07 search_records 双路融合接线【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 构造 `RAGSystem`（mock embeddings/vectorstore/bm25/reranker 全桩），`postgres_dsn` 注入 mock PG 检索器；向量路返回 `[d1, d2]`，关键词路返回 `[d2, d3]` |
| 步骤 | 调用 `search_records("q", k=2)`，断言 `rrf_fuse` 被调用且收到两路列表 |
| 预期 | 融合结果进入后续 rerank/parent 逻辑；`seen_hashes` 旧去重代码不再存在（grep 确认） |

### T1.2-08 PG 不可用降级 BM25【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 同上，但 PG 检索器 `search` 抛异常；`enable_bm25=True` 且 bm25 有文档 |
| 步骤 | 调用 `search_records("q", k=2)` |
| 预期 | 关键词路由 BM25 结果；全程无异常抛出 |

## 4. T1.3 qwen3-rerank 专用重排（新增 test_rag_rerank.py）

### T1.3-01 正常重排映射【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 4 个 Document；mock dashscope `TextReRank.call` 返回 `status_code==200`，`output.results=[{"index":2,"relevance_score":0.9},{"index":0,"relevance_score":0.7}]` |
| 步骤 | `DashScopeReranker("k").rerank("q", docs, top_k=3)` |
| 预期 | 返回 `[docs[2], docs[0]]`，顺序与 relevance 一致，长度 ≤ top_k |

### T1.3-02 候选截断保护【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 30 个 Document；mock call 记录收到的 documents 数量 |
| 步骤 | `rerank("q", docs30, top_k=5)` |
| 预期 | 发送给 API 的文档数为 20（`MAX_DOCS`）；单文档文本长度截断至 2000 字符（构造超长文档验证） |

### T1.3-03 API 异常触发降级链【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | `RAGSystem` 测试桩：`reranker_model.rerank` 抛 `RerankUnavailable`；`LLMReranker.rerank`（mock）返回 `[d2, d1]` |
| 步骤 | 调用 `RAGSystem._rerank("q", [d1, d2], top_k=2)` |
| 预期 | 返回 LLM 重排结果；无异常 |

### T1.3-04 SDK 网络异常转 RerankUnavailable【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | mock dashscope call 抛 `Exception("timeout")` |
| 步骤 | `DashScopeReranker("k").rerank("q", docs)` |
| 预期 | 抛出 `RerankUnavailable`（由上层捕获降级），不抛原始异常 |

### T1.3-05 API 返回非 200【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | mock 返回 `status_code=429` |
| 步骤 | 同上 |
| 预期 | 抛 `RerankUnavailable` |

### T1.3-06 结果为空不降级【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | mock 返回 200 但 `output.results=[]` |
| 步骤 | `rerank("q", docs)` |
| 预期 | 返回 `[]`（正常业务语义，不触发 LLM 降级——通过 `_rerank` 桩验证降级路径未被调用） |

### T1.3-07 配置开关【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | `enable_rerank_model=False` 构造 RAGSystem |
| 步骤 | 检查 `reranker_model is None`；`_rerank` 直接走 LLM 路径 |
| 预期 | 专用重排类未被实例化 |

## 5. T1.4 证据评分 LLM 融合（新增 test_evidence_scorer.py）

### T1.4-01 融合公式计算【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | mock LLM invoke 返回 JSON：`[{"source_id":"WEB1_1-1","score":0.8,"reason":"内容具体"}]`；记录 prior=0.58（普通域名） |
| 步骤 | `EvidenceScorer(mock_llm, prior_weight=0.4).score_batch([{source_id:"WEB1_1-1", domain:"example.com", source_type:"web", title:"t", snippet:"s"}])` |
| 预期 | `reliability_score == 0.4*0.58 + 0.6*0.8`（pytest.approx）；`reliability_reason == "内容具体（先验0.58）"` |

### T1.4-02 LLM 输出带 markdown 包裹仍可解析【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | mock 返回 `"```json\n[...]\n```"` 包裹的合法 JSON |
| 步骤 | 同上调用 |
| 预期 | 正常解析融合，结果同 T1.4-01 |

### T1.4-03 部分证据缺评回退先验【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 两批输入，LLM 只返回其中一条的评分 |
| 步骤 | `score_batch([r1, r2])` |
| 预期 | r1 融合分；r2 纯先验分且 reason 为先验理由 |

### T1.4-04 非法分数 clamp【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | LLM 返回 `score: 1.7` 与另一条 `score: -0.5` |
| 步骤 | 融合计算 |
| 预期 | LLM 分被 clamp 到 [0,1] 后参与融合；最终分数在 [0,1] |

### T1.4-05 LLM 整体异常回退先验【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | mock invoke 抛异常 |
| 步骤 | `score_batch([r1, r2])` |
| 预期 | 两条均返回先验分数与先验理由，无异常抛出 |

### T1.4-06 批量分批与上限【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | 25 条记录；mock invoke 记录调用次数 |
| 步骤 | `score_batch(25 条)` |
| 预期 | invoke 被调用 2 次（20+5） |

### T1.4-07 local 证据走融合仍高可信【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | local 记录（`source_type=="local"`，prior 0.92），LLM 评 0.7 |
| 步骤 | 融合 |
| 预期 | 分数介于 0.7~0.92 之间且 ≥0.6，不触发 low_confidence audit_flag |

### T1.4-08 融合分数驱动审计标记【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | mock LLM 给 web 证据评 0.2（prior 0.58 → 融合约 0.35）；`evidence_llm_fusion=true` |
| 步骤 | 走 `_fallback_audit`（或实际接入点）后检查 `audit_flags` |
| 预期 | 该证据出现在 `low_confidence` audit_flags；开关置 false 时同证据不出现（先验 0.58 也不达标则用 0.72 媒体先验证反向用例） |

### T1.4-09 prompt 结构校验【P2】

| 项 | 内容 |
| --- | --- |
| 前置 | 捕获 mock invoke 的 prompt 文本 |
| 步骤 | 断言 prompt 含 query、每条证据的 source_id 与 title、以及「只输出 JSON 数组」约束 |
| 预期 | 格式与 R1.4 文档 3.2 定义一致 |

## 6. 集成与浏览器自动化验证（阶段一收尾统一执行）

> 以下用例通过 Playwright 浏览器自动化执行，脚本存放于 `agent_front/e2e/phase1_retrieval.spec.ts`。
> 前置：后端服务运行在 `http://127.0.0.1:8000`，前端运行在 `http://localhost:5173`（或 8080），中间件（PG/Redis/Milvus/RabbitMQ）已启动。
> 选择器参考：`textarea.composer-input`（输入框）、`button.send-btn`（发送）、`.source-list .source-item`（来源条目）、`.message-item`（消息气泡）。

| 编号 | 场景 | 自动化步骤 | 预期 |
| --- | --- | --- | --- |
| M1-01 | 端到端检索冒烟 | 1. 导航至 `/knowledge` 页面<br>2. 点击 `.upload-dropzone` 触发文件选择，上传一份含专有名词的 MD 文档<br>3. 等待 `.upload-task-list` 中该文档状态变为 `indexed`（轮询至 `stats-cards` 显示 pending=0）<br>4. 导航至 `/chat` 页面<br>5. 在 `textarea.composer-input` 输入该专有名词相关问题<br>6. 点击 `button.send-btn` 或按 Enter 发送<br>7. 等待 `.message-item.assistant` 出现且内容非空<br>8. 检查 `.source-list .source-item.kb` 是否包含该文档标题 | 报告引用包含该文档（Parent-Child 扩展后父块上下文）；PG 日志可见 tsvector 查询 |
| M1-02 | Web 搜索降级冒烟 | 1. 通过 `page.route` 拦截后端 `GET /api/v1/admin/config`，mock 响应中 `search_providers` 为 `["tavily"]` 且无 Key<br>2. 导航至 `/chat` 页面<br>3. 在输入框提问需联网的问题<br>4. 点击发送并等待响应完成<br>5. 检查 `.message-item.assistant` 内容非空（报告仍产出）<br>6. 捕获浏览器 console 日志，断言含 warning 级别消息或通过后端日志验证 | 检索静默失败走默认链，报告仍产出，日志有 warning |
| M1-03 | rerank 真实冒烟（有 Key 时） | 1. 导航至 `/knowledge` 页面，上传知识库文档并等待 `indexed`<br>2. 导航至 `/chat` 页面<br>3. 在输入框提问本地知识相关问题<br>4. 点击发送，等待 `.agent-timeline` 出现 `write` 节点 `done` 状态<br>5. 检查 `.message-item.assistant` 正常渲染<br>6. 通过 `page.route` mock 配置使 rerank Key 为空后重复提问<br>7. 检查流程仍成功完成（`run.completed` 事件） | 响应正常；日志无 `RerankUnavailable`；关闭 Key 后出现降级 warning 但流程成功 |
| M1-04 | 证据评分融合冒烟 | 1. 导航至 `/chat` 页面<br>2. 在输入框提问混合 web+本地知识的问题<br>3. 点击发送并等待响应完成<br>4. 检查 `.source-list` 渲染，展开来源列表（点击 `.source-header`）<br>5. 对每个 `.source-item` 检查 `title` 属性是否含 `reliability_reason` 相关文本<br>6. 断言至少部分来源的 `title` 或 `data-*` 属性含「先验」关键字 | 报告来源面板各证据带 `reliability_reason`，其中部分含「（先验x.xx）」后缀 |

## 7. 回归范围

- `python -m pytest app/test -q` 全量
- 重点观察既有用例：`test_citations.py`、`test_evidence_queries.py`、`test_search_provider.py`、`test_p1_smoke.py`
- 若 `test_p1_smoke.py` 或其他集成用例因检索链路变化失败：确认失败原因属于本阶段设计变更（如 RRF 替代 hash 去重）则更新断言；属于意外破坏则修复后重跑
