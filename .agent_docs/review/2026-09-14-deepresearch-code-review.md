# DeepResearch 项目审查报告

| 项 | 值 |
| --- | --- |
| 审查日期 | 2026-09-14 |
| 审查范围 | 全仓（`app/` 后端 88 个 py 文件 / 21313 行；`agent_front/` 前端；工程配置与脚本） |
| 审查方式 | 静态阅读 + 依赖源码交叉验证 + 实机运行测试套件 |
| 实测测试结果 | `conda llmdev` (Python 3.11.15)：**441 passed, 2 skipped, 7 warnings in 48.92s** |
| 问题总数 | 24（P0 ×2、P1 ×4、P2 ×9、P3 ×9） |
| 总体结论 | **架构设计成熟、工程完成度高；但存在 2 个阻断级问题（凭据入库、全站无鉴权），上线前必须处理** |

---

## 一、项目概览（审查对象）

多智能体深度研报助手：`LangGraph` 拓扑（intent → clarify → plan → [web_search ∥ local_rag] → deep_dive → analyze →(reflect|write)→ END）+ `FastAPI` SSE 流式 + `Vue3/Pinia` 前端；中间件为 PostgreSQL(pgvector) / Redis / Milvus / MinIO / RabbitMQ。

**值得肯定的设计**（这些不是客套，是审查中确认为真实优势的点）：

- **SSE 事件协议有单一事实源**：`docs/event-protocol.json` + `app/backend/schemas/events.py` 的 `EVENT_REGISTRY`，事件类型与 pydantic schema 一一对应，且前端 `types/events.gen.ts` 由脚本生成，协议漂移风险被结构性压低。
- **三不变式有显式文档并落到代码**：「流一定结束（completed/cancelled/error 必居其一）」「delta 顺序拼接」「前端忽略未知 type」，且 `stream_research` 刻意不写 `finally`（注释指出这是为了规避旧实现的 NameError 挂起）—— 这是一次有意识的、有理由的设计决策，不是疏漏。
- **Outbox Pattern 落地扎实**：`documents + document_chunks + chunk_sync_messages` 在同一 PG 事务写入，MQ 发送失败由启动补偿扫描兜底，`retry_failed_chunks` 还会主动重建消息而非只依赖启动扫描。
- **HITL 与崩溃恢复闭环完整**：3 个 interrupt 点 + `scan_orphans` 重启扫描 + Redis 拘留标记 + `/threads/{id}/interrupt` 前端重建，链路是通的。
- **会话元数据独立成表**（`chat_threads`）的决策理由被完整写进注释（checkpoints 无 user_id / 无序 / 无展示字段），这是踩过坑后回到根因的正确做法。
- **checkpointer 降级链**（Postgres → Redis → 内存）且明确区分 async/sync 两套工厂，`.agent_test/gate/report.md` 记录了 P2-2 实机验证。

---

## 二、P0 — 阻断级（上线前必须处理）

### P0-1 中间件凭据已进入版本库

**证据**

- `config.json`（**被 git 跟踪**，`git ls-files` 确认）含明文口令：
  - `postgres_dsn: postgresql://root:postgres123@localhost:5432/mydb`
  - `redis_url: redis://:redis123456@localhost:6379`
  - `minio_access_key / minio_secret_key: minioadmin`
  - `rabbitmq_url: amqp://admin:admin123456@localhost:5672/`
- `docker-compose.app.yml` 再次硬编码同一批口令（`redis123456` / `postgres123` / `admin123456`），同样被跟踪。
- `README.md:92` 明确写着 `config.json  # 业务配置 (无敏感信息)` —— **文档与事实直接矛盾**。
- `.gitignore` 只忽略了 `.env` / `.env.local` / `.env.*.local`，未覆盖 `config.json`。

**影响**：仓库一旦外发、开源或多人共享，等同于泄露全部中间件凭据；由于 `docker-compose.middleware.yml` 使用同一批口令，攻击者可直接接管数据库与消息队列。

**建议**

1. 轮换全部中间件口令（视为已泄露）。
2. `config.json` 只保留业务项（model / max_iterations / hitl_* / 各类开关），连接串与密钥全部移入 `.env`。
3. `docker-compose.app.yml` 改为 `POSTGRES_DSN=${POSTGRES_DSN}` 形式引用环境变量。
4. 清理 git 历史（`git filter-repo` 或 BFG），否则凭据仍留在历史提交里。
5. 修正 README 中"无敏感信息"的表述。

---

### P0-2 全站无鉴权 + 越权访问（IDOR）

**证据**：全仓 grep `Authorization / Bearer / HTTPBearer / Security( / current_user / X-User`，除 `rag/core.py` 调用 DashScope 外**零命中**。所有业务接口的 `user_id` 完全由调用方自由指定：

| 接口 | 越权点 |
| --- | --- |
| `GET /api/v1/research/threads/{thread_id}/messages` | 无 `user_id` 参数，**任意人可读任意会话全文** |
| `DELETE /api/v1/research/threads/{thread_id}` | `user_id` 是查询参数，传任意值即可删除他人会话 |
| `GET /api/v1/research/memories` | `user_id` 为查询参数，可枚举他人长期记忆 |
| `DELETE /api/v1/documents/{doc_id}` | **完全无 `user_id` 参数**（对比 `/documents/batch` 有校验，两条路径不一致） |
| `POST /api/v1/admin/config/reload` | `_check_admin_token`：`if expected and x_admin_token != expected` —— `ADMIN_TOKEN` 为空（默认值，`.env.example` 亦留空）时**直接放行**，可热改 `model`、`search_providers` 等运行参数 |

**影响**：任何能访问 8000 端口的客户端可读、删、改任意用户数据，并篡改服务运行配置。在 Docker 部署（端口直接映射 `8000:8000`）场景下等于对外裸奔。

**建议**

1. 引入统一认证依赖（FastAPI `Depends`），从 token 解析出主体 `user_id`，**禁止从请求参数取 user_id**；`ThreadRepository` / `ChunkRepository` 已支持按 `user_id` 过滤，改造面不大。
2. `delete_document` 补 `user_id` 校验，与 `delete_documents_batch` 对齐。
3. `_check_admin_token` 改为 fail-closed：`expected` 为空时**拒绝**并提示配置 `ADMIN_TOKEN`，而不是放行。

---

## 三、P1 — 严重

### P1-1 长期记忆「写」与「读」命名空间不一致，跨会话记忆实际失效

**证据**

- 写入侧 `app/backend/service/memory_service.py:69`：`namespace=("memories", "{user_id}")`
- 读取侧同文件 `:93`（`hot_path_search`）与 `:252`（`list_memories`）：`namespace = (user_id, "memories")`
- 已交叉验证 langmem 实现：`langmem/utils.py:15` `NamespaceTemplate` 会按 `configurable` 格式化模板，其 docstring 明确示例 `NamespaceTemplate(("org", "{user_id}"))` → `('org', 'alice')`。即写入落在 `memories/<user_id>`，而读取查询前缀 `<user_id>/memories`。

**影响**：langmem 后台提取的用户偏好/研究主题**永远检索不到**。`hot_path_search` 只会命中 `put_memory()` 手动写入的条目（它用的是读侧命名空间），因此 P5「跨会话记忆」这条链路在生产路径上实际是断的。测试没覆盖到这个不一致——`test_memory.py` 只测了 `put_memory + list_memories`（两者同命名空间，自然通过）。

**建议**：统一为 `(user_id, "memories")`（与 `put_memory` 一致），并在 `test_memory.py` 增加一条「manager 写入 → hot_path_search 能检索到」的端到端断言，否则这个 bug 会再次静默回归。

---

### P1-2 删除文档不清理向量索引，已删内容仍可被检索

**证据**：`app/backend/service/document_service.py:637-660`

```python
"""删除文档（PG + MinIO）。

注意: Milvus 中的向量索引需要异步清理（发送删除消息到 MQ）。
"""
object_key = self._repo.delete_document(doc_id)   # PG 级联删 chunks
...
self._minio.delete_file(object_key)
return {..., "message": "文档已删除（PG + MinIO），向量索引需异步清理"}
```

注释声明要「发送删除消息到 MQ」，但函数体内**没有任何 MQ 发布调用**，`ChunkSyncConsumer` 也只有新增/更新路径，没有删除路径。

**影响**：Milvus 向量与 BM25 索引成为孤儿。用户以为已删除的敏感文档，仍会出现在 `local_rag` 检索结果并被引用进报告——既是数据一致性问题，也是数据合规问题。

**建议**：补一条 `chunk.sync.delete.{doc_id}` 路由与消费分支（含幂等与重试），或在删除时同步调用 Milvus `delete(expr=f'doc_id == "{doc_id}"')` + 重建 BM25；把返回值里的「需异步清理」改成事实描述。

---

### P1-3 全局异常处理器把内部异常详情回吐客户端

**证据**：`app/app_main.py:329-335`

```python
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception(...)
    return JSONResponse(status_code=500, content={"detail": f"Internal Server Error: {exc}"})
```

**影响**：`str(exc)` 可能包含数据库 DSN、文件绝对路径、SQL 片段、第三方 SDK 报错细节，属于典型信息泄露面。结合 P0-2（无鉴权），任何未授权访问者都能直接读到这些细节。

**建议**：对外只返回通用错误码与追踪 ID（如 `{"detail": "Internal Server Error", "trace_id": ...}`），细节仅落日志。

---

### P1-4 `tools.py` 整层工具未接线，且夹杂约 20 个纯占位桩函数

**证据**

- `app/mult_agents/runtime.py:104-114`：`build_agents` 对全部 9 个 agent 传 `tools=[]` —— 即 `intent_router / planner / scout_web / scout_local / evidence_judge / analyst / direct_responder / writer / clarifier` **都没有绑定任何工具**。
- `app/mult_agents/tools.py`（653 行）中的 `@tool` 定义因此全部处于「已定义、无人调用」状态：`search_knowledge_base`、`web_search_stub`、`simple_calculator`、`safe_read_file/safe_write_file/safe_move_file`、`optimize_query`、`explain_term` 等。
- 其中约 20 个是纯占位返回字符串，例如 `amap_weather` → `"未配置高德API，收到天气查询: {city}"`、`sql_inter` / `python_inter` / `fig_inter` / `execute_terminal_command` / `news_search_stub` / `finance_search_stub` / `extract_url_content_stub` 等。
- 附带风险：`safe_write_file` / `safe_move_file` 具备真实文件写能力（`_workspace_root()` 默认 `/workspace`，Windows 下解析为盘符相对路径），一旦将来被接入 agent，就是一个未被评估过的写入面。

**影响**：直接违反 `AGENTS.md` 的「不留痕迹」铁律；读者会误以为这些工具在链路上生效（`web_search_stub` 的 docstring 还写着「DuckDuckGo / SearXNG 多源降级」，而真正生效的是节点内直接调用的 `web_search_records()`）。也拖累了工具层的可维护性——改一处不知影响面。

**建议**：确认「节点直接调用 Python 函数、不走 agent tool-calling」是既定架构后，**整块删除**未接线的 `@tool` 定义与桩函数；`tools.py` 只保留 Provider 链、`init_rag_system`、`web_search_records`、`search_knowledge_base_records` 这些真正被引用的函数。若确实计划未来启用工具调用，则改为独立模块并显式标注「未启用」。

---

## 四、P2 — 中等

| # | 问题 | 证据 | 影响与建议 |
| --- | --- | --- | --- |
| P2-1 | **摘要压缩会累积重复消息** | `research_service.py:721` / `:751` 用 `aupdate_state(config, {"chat_messages": compressed_msgs})`，而 `chat_messages` 的 reducer 是 `add_messages`（`state.py:19`） | 新建的 `SystemMessage` 无 ID，`add_messages` 会分配新 ID 并**追加**而非替换。长会话每次触发压缩都会多留一份摘要消息，上下文逐渐被重复摘要挤占。建议改用「按 ID 显式删除 + 插入」或维护稳定的摘要消息 ID |
| P2-2 | **`config_path` 是死参数，且相对路径会解析错** | `config.py:90` `from_file(path)` 形参从未被使用（内部只调 `get_business_settings()`）；`research_service.py:1089` `os.getenv("CONFIG_PATH", "app/config.json")` | 按 README 的 `cd app && uvicorn app_main:app` 启动，相对路径会解析成 `app/app/config.json`（不存在）。当前靠「忽略参数」侥幸无碍，但语义已坏，任何一次"把 path 用起来"的改动都会立刻踩雷。建议删除该参数，统一走 `AppSettings().config_path` |
| P2-3 | **interrupt 协议默认值与 schema 约束不一致** | `events.py:77` `kind: Literal["plan_approval","clarification","report_review"]`；`research_service.py:355` payload 缺 kind 时填 `"unknown"` | `"unknown"` 过不了 Literal 校验 → `event()` 抛 ValidationError → 整个 SSE 流被外层 `except` 转成 `run.error`。建议 Literal 补 `"unknown"`，或让缺 kind 时降级为 `agent.status` 而非硬失败 |
| P2-4 | **父块内容取成了子块内容** | `chunk_consumer.py:158-168` 与 `document_service.py:535-545` 均用 `page_content=chunk.content`（子块文本）构造 `chunk_type="parent"` 文档 | 父块退化为子块副本，父子分块「用父块补全上下文」的设计意图失效，检索质量下降。需在 payload 中携带真实父块文本 |
| P2-5 | **跨模块访问私有属性，同一逻辑两处实现** | `research_router.py:55-65` 直接读写 `registry._tasks` 并手工拼 `RunningTask`；`app_main.py:297-303` 直接访问 `registry._subscriber_task` / `registry.redis`；`chunk_consumer.py:157` / `document_service.py:534` 访问 `rag._parent_map`；`research_service.py:710/744` 访问 `summary_service._threshold` | `TaskRegistry.register()` 里还负责写 Redis `cancel:{thread_id}=running` 标记，而 router 的旁路实现跳过了它 —— 多 worker 兜底标记因此失效。建议把注册逻辑收敛回 `register()`，或提供公开 API；`_threshold` 改为公开只读属性 |
| P2-6 | **后台任务未持有引用，可能被 GC 静默回收** | `research_service.py:651-653` `loop.create_task(_do_title())`；`task_registry.py:306-307` `loop.create_task(...)` 均不保存引用 | 与 asyncio 官方文档警告的场景一致：任务可能在执行前被回收，标题生成/Redis 清理随机失效且无日志。`MemoryService` 已用 `_background_tasks` 集合正确规避（`memory_service.py:52`），说明团队已知此模式，属遗漏。建议统一 |
| P2-7 | **上传大小限制形同虚设** | `document_router.py:60-70`：先 `content = await file.read()` 全量读入内存，再判断 `len(content) > MAX_FILE_SIZE_BYTES` | 50MB 上限拦不住内存占用，恶意大体积请求可造成内存压力。建议先按 `Content-Length` 拒绝，或分块累计读取并在超限时中断 |
| P2-8 | **对外宣告了一个不存在的约束** | `document_router.py:35` `MAX_FILES_PER_BATCH = 20`，仅被 `GET /documents/extensions` 返回 | 前端可能据此设计批量上传，但项目里没有任何批量上传接口。建议删除该常量，或补齐接口 |
| P2-9 | **死代码 / 名不副实的实现** | `settings.py:199` `_SENSITIVE_FIELDS: set[str] = set()` 恒空 → `admin_router.py:33` 的「脱敏视图」实际不脱敏；`state.py:94` `ResearchStateCompat = AgentState` 无任何引用；`store_client.py:68` `from psycopg_pool import AsyncConnectionPool` 导入未使用 | 注释与行为不符会误导后续维护。当前 BusinessSettings 恰好无密钥字段，故无实害，但一旦往 business 配置里加敏感项，脱敏就会静默失效 |

---

## 五、P3 — 轻微 / 工程卫生

| # | 问题 | 证据 |
| --- | --- | --- |
| P3-1 | **前端目录混入后端 Python 工程文件**（且被 git 跟踪） | `agent_front/pyproject.toml`（`name = "mult_agents"`）、`agent_front/requirements.txt`（整份 pip freeze） |
| P3-2 | **`output/` 混入与项目无关的产物** | `output/extract_xlsx.py`、`output/gen_filtered_csv.py`、`output/可投递企业清单.txt`、`output/e2e_*.png`；虽被 gitignore，仍污染工作区 |
| P3-3 | **README 测试数据过期，覆盖率门禁实际未生效** | README 称 `269 passed, 2 skipped`，实测 **441 passed, 2 skipped**；`pyproject.toml` 配了 `fail_under = 80` 但 `addopts` 未启用 `--cov`，README 中的 `--cov` 是手动参数 → 门禁不会自动拦截 |
| P3-4 | **前端无法一键跑测试** | `package.json` 无 `test` 脚本，README 却指导 `npx vitest`；`vitest.config.ts` 与 `test/`（4 个用例文件）均在位 |
| P3-5 | **声明的 pytest 标记无实际用例** | `pyproject.toml` `markers = [llm, slow, offline]` + `addopts = -m "not llm and not slow"`，但全仓无 `pytest.mark.llm` / `pytest.mark.slow` 使用 → 当前是空转（无害，但标记形同虚设） |
| P3-6 | **`.trash/` 未清空** | `.trash/SSE面试准备.md` 残留，`AGENTS.md` 要求交付前 `.trash/` 为空 |
| P3-7 | **重复代码与重复导入** | `research_service.py:38` `import asyncio as _asyncio` 与顶部 `:14 import asyncio` 重复；`stream_research` 与 `resume_stream` 的事件分发逻辑约 200 行近乎逐行重复；`run` / `run_with_route` / `stream_research` 三处的「热路径记忆检索 + 初始状态构造」重复三遍 |
| P3-8 | **日志字段恒为空** | `write.py:118` 打印 `state.get("thread_id", "")`，但 `AgentState` 没有 `thread_id` 字段 → 该字段永远输出空串 |
| P3-9 | **`.env.example` 缺键且含误导键** | 缺 `MINIO_ENDPOINT/ACCESS_KEY/SECRET_KEY/BUCKET`、`RABBITMQ_URL`（config.json 实际在用）；却列了 `MEMORY_TOP_K`，而代码读的是 `MEMORY_HOT_PATH_TOP_K`（`config.py:134`） |

---

## 六、测试与验证实测记录

| 检查项 | 命令 | 结果 |
| --- | --- | --- |
| 后端测试（真实依赖环境） | `conda llmdev` → `python -m pytest app/test/ -q` | **441 passed, 2 skipped, 7 warnings in 48.92s** ✅ |
| 与 README 声称比对 | README 表格 | ❌ 不一致（README 269/2，实测 441/2） |
| 跳过用例 | — | `test_p1_smoke.py` 2 例（需真实 LLM + PG，已知） |
| 测试告警 | — | 3 处 `coroutine ... was never awaited`（`test_p3.py` / `test_search_provider.py`），属测试自身写法问题，非产品缺陷；另有 2 处 pytest 临时目录清理失败（Windows 路径问题，无害） |
| 前端测试 | `npx vitest` | 未执行（`package.json` 无对应脚本，见 P3-4） |
| 项目自建门禁 | `.agent_test/gate/report.md` | `GATE_STATUS=PASSED`，但报告日期为 2026-09-05、commit `cfef238`，**已落后于当前代码**（当前 HEAD `cd098b3`），且其中「reviewer 复检」一栏仍为空 |

> 说明：本报告未运行前端 vitest，也未做浏览器端 E2E 联调（`agent-browser` 在当前 Windows 环境不可用）。上述未覆盖部分均已在结论中标注，未当作"通过"处理。

---

## 七、修复优先级建议

**上线前（阻断）**
1. P0-1 凭据轮换 + 移出仓库 + 清理 git 历史
2. P0-2 统一鉴权，杜绝从请求参数取 `user_id`；`ADMIN_TOKEN` 改 fail-closed

**紧随其后（严重，影响功能正确性）**
3. P1-1 统一记忆命名空间，并补一条端到端记忆断言（这是唯一一个"功能声称可用但实际断了"的问题）
4. P1-2 补文档删除的向量清理链路
5. P1-3 异常详情不外泄
6. P1-4 清理 `tools.py` 未接线工具与桩函数

**技术债清理（建议随迭代消化）**
7. P2-1 摘要重复消息（会随会话变长而恶化，优先级在 P2 中最高）
8. P2-5 私有属性越界访问 —— 它直接导致多 worker 取消兜底失效
9. P2-4 父块内容错误（直接影响检索质量）
10. P3 系列作为一次「纯净原则」专项清理（删桩、删死代码、合并重复实现、修正文档）

---

## 八、审查局限

> ⚠️ 本节的「未逐行审查 rag/core.py 与 _evidence.py」一项**已由第二篇补齐**，见
> `.agent_docs/review/2026-09-14-deepresearch-code-review-part2.md`（新增 22 项问题，累计 46 项）。
> 第二篇在其中确认了 4 条「宣称可用但实际断链」的链路，优先级高于本篇多数条目。

- 未执行端到端联调（浏览器自动化在当前环境不可用），因此**运行期**问题（Milvus 检索精度、SSE 长连接稳定性、PDF 导出实际效果）不在本次结论范围内。
- 未验证 git 历史中凭据的实际暴露范围（未执行 `git log -p -- config.json`），仅确认当前工作区被跟踪。
- 本篇的 P2-4（父块内容取成子块内容）根因已在第二篇 P2-16 定位为「父子切分逻辑重复实现」，修法以第二篇为准。
