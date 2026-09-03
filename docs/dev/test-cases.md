# DeepResearch 测试用例文档

> 按功能模块组织的测试用例清单，覆盖后端 API、前端交互、事件协议、记忆系统等核心功能。

## 1. 后端健康检查与基础设施

| 用例 ID | 模块 | 描述 | 前置条件 | 验证步骤 | 预期结果 |
|---------|------|------|----------|----------|----------|
| TC-HC-01 | 健康检查 | GET /health 返回 200 | 后端启动 | GET http://localhost:8000/health | 200, {status:"ok"} |
| TC-HC-02 | 健康检查 | GET /health/live 返回 200 | 后端启动 | GET http://localhost:8000/health/live | 200, {status:"ok"} |
| TC-HC-03 | 事件循环 | Windows 下 SelectorEventLoop 正确设置 | Windows 平台 | 检查后端日志无 ProactorEventLoop 警告 | PostgresStore 初始化成功 |
| TC-HC-04 | 记忆系统 | P5 记忆系统初始化不超时 | PG+Redis 运行 | 检查日志 "P5 记忆系统初始化完成" | 无 30s 超时 |

## 2. 后端研究 API

| 用例 ID | 模块 | 描述 | 前置条件 | 验证步骤 | 预期结果 |
|---------|------|------|----------|----------|----------|
| TC-RS-01 | 会话列表 | 获取空会话列表 | 无会话 | GET /api/v1/research/threads?user_id=default_user | 200, {threads:[], total:0} |
| TC-RS-02 | 会话列表 | 创建会话后列表更新 | 先发送一条消息 | GET /api/v1/research/threads | 200, total>=1 |
| TC-RS-03 | 会话消息 | 获取不存在线程的消息 | 虚拟线程 ID | GET /api/v1/research/threads/welcome/messages | 200, {messages:[]} |
| TC-RS-04 | 历史快照 | 虚拟线程 ID 不报 500 | welcome 虚拟线程 | GET /api/v1/research/history/welcome | 200, {history:[]} |
| TC-RS-05 | 流式研究 | 发送研究请求获得 SSE 流 | 后端正常 | POST /api/v1/research/stream | SSE 事件流，含 run.started/delta/completed |
| TC-RS-06 | 取消研究 | 取消运行中的任务 | 有运行中任务 | POST /api/v1/research/cancel | 200, {cancelled:true} |
| TC-RS-07 | 恢复研究 | HITL 恢复 | 有 interrupt 状态 | POST /api/v1/research/resume | SSE 流继续 |
| TC-RS-08 | 并发拦截 | 同一线程重复运行返回 409 | 有运行中任务 | POST /api/v1/research/stream (同 thread_id) | 409 Conflict |

## 3. 记忆系统 (P5)

| 用例 ID | 模块 | 描述 | 前置条件 | 验证步骤 | 预期结果 |
|---------|------|------|----------|----------|----------|
| TC-MEM-01 | PostgresStore | init_store 单例模式 | 已初始化 | 再次调用 init_store | 返回同一实例 |
| TC-MEM-02 | PostgresStore | close_store 清理连接 | 已初始化 | 调用 close_store | _store_instance 为 None |
| TC-MEM-03 | 热路径检索 | 无记忆时返回空字符串 | Store 无数据 | hot_path_search("user1","query") | "" |
| TC-MEM-04 | 后台提取 | 触发后台提取不阻塞主流程 | mock store | trigger_background_extract | 立即返回，异步执行 |
| TC-MEM-05 | 记忆写入 | put_memory 写入成功 | Store 已初始化 | put_memory("user1","text","general") | 返回 key |
| TC-MEM-06 | 记忆列表 | list_memories 返回格式正确 | 有记忆数据 | list_memories("user1") | [{id,text,kind,created_at}] |
| TC-MEM-07 | 记忆 API | GET /memories 接口可用 | 后端启动 | GET /api/v1/research/memories | 200, {memories:[],total:0} |

## 4. 事件协议 (SSE)

| 用例 ID | 模块 | 描述 | 前置条件 | 验证步骤 | 预期结果 |
|---------|------|------|----------|----------|----------|
| TC-EVT-01 | 事件类型 | 10 种事件类型定义完整 | 检查 events.gen.ts | EventType 联合类型包含全部 10 种 | 类型匹配 |
| TC-EVT-02 | SSE 帧解析 | 正常帧解析 | 完整 SSE 帧 | takeSseLines("data:{json}\n\n") | 返回 [json] |
| TC-EVT-03 | SSE 帧解析 | 半帧缓冲 | 跨 chunk 的半行 JSON | 模拟分块输入 | 正确拼接完整帧 |
| TC-EVT-04 | SSE 帧解析 | 空行跳过 | 帧间空行 | takeSseLines("data:{}\n\n\n\ndata:{}") | 返回 2 个 |
| TC-EVT-05 | 未知事件 | 未知 type 静默忽略 | 未知事件 | dispatch 未知类型 | 不报错，不修改状态 |
| TC-EVT-06 | run.started | 事件触发 chat.startAssistantMessage | mock store | dispatch run.started | streaming 消息创建 |
| TC-EVT-07 | run.completed | 事件触发 threads.refresh | mock fetch | dispatch run.completed | chat.finish + refresh 调用 |
| TC-EVT-08 | run.error | 事件触发 chat.markError | mock store | dispatch run.error | 消息标记为 error |

## 5. 前端对话界面

| 用例 ID | 模块 | 描述 | 前置条件 | 验证步骤 | 预期结果 |
|---------|------|------|----------|----------|----------|
| TC-UI-01 | 页面渲染 | 首页加载后显示对话界面 | 前端 dev server 运行 | 访问 http://localhost:5173/chat | 显示侧边栏+对话区+输入框 |
| TC-UI-02 | 页面渲染 | 欢迎面板显示 4 个预设提示 | 空会话状态 | 检查 .starter-card 数量 | 4 个卡片可见 |
| TC-UI-03 | 页面渲染 | 输入框初始为空且禁用发送 | 页面加载 | 检查输入框和发送按钮 | 输入框空，发送按钮 disabled |
| TC-UI-04 | 路由 | 根路径重定向到 /chat | 访问 / | 检查 URL | 重定向到 /chat |
| TC-UI-05 | 路由 | /knowledge 路由可达 | 访问 /knowledge | 检查页面内容 | 知识库页面渲染 |
| TC-UI-06 | 侧边栏 | 新建会话按钮可点击 | 页面加载 | 点击 "新建会话" | 触发 requestNewChat |
| TC-UI-07 | 会话历史 | 空状态显示提示文案 | 无会话 | 检查侧边栏文案 | "还没有会话..." 可见 |
| TC-UI-08 | RollbackMenu | 虚拟线程 ID 不查询后端 | threadId=welcome | 检查网络请求 | 无 /history/welcome 请求 |

## 6. 前端 Store 状态管理

| 用例 ID | 模块 | 描述 | 前置条件 | 验证步骤 | 预期结果 |
|---------|------|------|----------|----------|----------|
| TC-ST-01 | ChatStore | ensureThread 设置 currentThreadId | 初始状态 | ensureThread("t1") | currentThreadId="t1" |
| TC-ST-02 | ChatStore | addUserMessage 添加用户消息 | 线程已创建 | addUserMessage("t1","hello") | messages 长度+1 |
| TC-ST-03 | ChatStore | startAssistantMessage 创建流式消息 | 线程已创建 | startAssistantMessage("t1") | streaming 消息 status=streaming |
| TC-ST-04 | ChatStore | appendDelta 拼接文本 | 有 streaming 消息 | appendDelta("t1",mid,"text") | content 包含 "text" |
| TC-ST-05 | ChatStore | markCancelled 标记取消 | 有 streaming 消息 | markCancelled("t1") | status=cancelled, running=false |
| TC-ST-06 | ChatStore | markError 标记错误 | 有 streaming 消息 | markError("t1",{code,msg}) | status=error |
| TC-ST-07 | ChatStore | 切线程不丢消息 | 两个线程各有消息 | getMessages("t1") 后切到 "t2" | t1 消息仍在 |
| TC-ST-08 | ThreadsStore | load 从后端获取列表 | mock fetch | load() | threads 更新 |
| TC-ST-09 | ThreadsStore | startNewThread 生成 ID | 初始状态 | startNewThread() | currentThreadId 以 "thread_" 开头 |
| TC-ST-10 | InterruptStore | raise 设置 interrupt | 初始状态 | raise("t1",{kind,payload}) | get("t1") 返回 interrupt |

## 7. HITL 人工干预

| 用例 ID | 模块 | 描述 | 前置条件 | 验证步骤 | 预期结果 |
|---------|------|------|----------|----------|----------|
| TC-HITL-01 | 中断检测 | interrupt.raised 事件触发卡片 | 有 interrupt 事件 | dispatch interrupt.raised | intr.get() 返回 interrupt |
| TC-HITL-02 | 恢复 | resume 发送 payload | 有 interrupt | resume("t1",{kind:"clarification",answer:"yes"}) | POST /resume 被调用 |
| TC-HITL-03 | kind 校验 | 不匹配 kind 返回 422 | interrupt kind=plan_approval | POST /resume with kind=clarification | 422 |
| TC-HITL-04 | 清除 | resume 后 interrupt 状态清除 | 有 interrupt | resume() | intr.get() 返回 null |

## 8. 知识库文档管理

| 用例 ID | 模块 | 描述 | 前置条件 | 验证步骤 | 预期结果 |
|---------|------|------|----------|----------|----------|
| TC-DOC-01 | 文档列表 | 获取空文档列表 | 无文档 | GET /api/v1/documents/list | 200, documents:[] |
| TC-DOC-02 | 文档统计 | 获取统计信息 | 无文档 | GET /api/v1/documents/stats | 200, 全零 |
| TC-DOC-03 | 扩展名 | 获取支持的扩展名 | 后端启动 | GET /api/v1/documents/extensions | 200, extensions 列表 |
| TC-DOC-04 | 上传 | 上传文档 | 有文件 | POST /api/v1/documents/upload | 200, doc_id 返回 |
| TC-DOC-05 | 删除 | 删除文档 | 有文档 | DELETE /api/v1/documents/{doc_id} | 200, deleted:true |

## 9. 配置与基础设施

| 用例 ID | 模块 | 描述 | 前置条件 | 验证步骤 | 预期结果 |
|---------|------|------|----------|----------|----------|
| TC-CFG-01 | 配置加载 | AppSettings 合并 .env 和 config.json | 有 .env 和 config.json | AppSettings() | 各字段正确合并 |
| TC-CFG-02 | 配置加载 | AppConfig.from_file 不缺 API key | .env 有 DASHSCOPE_API_KEY | AppConfig.from_file() | api_key 非空 |
| TC-CFG-03 | 配置加载 | 环境变量覆盖 config.json | 设 MODEL 环境变量 | AppConfig.from_file() | model 取环境变量值 |
| TC-CFG-04 | CORS | 允许 localhost:5173 | 默认配置 | 检查 cors_origins() | 包含 localhost:5173 |

## 10. Docker 部署

| 用例 ID | 模块 | 描述 | 前置条件 | 验证步骤 | 预期结果 |
|---------|------|------|----------|----------|----------|
| TC-DC-01 | 中间件 | docker-compose.middleware.yml 启动 | Docker 可用 | docker compose up -d | 6 个容器全部 healthy |
| TC-DC-02 | 应用 | docker-compose.app.yml 无 RabbitMQ 重复 | middleware 已启动 | 检查 services 列表 | 无 rabbitmq 服务定义 |
| TC-DC-03 | 健康检查 | backend healthcheck 使用 /health/live | 应用启动 | docker inspect healthcheck | test 含 /health/live |
| TC-DC-04 | 前端 | nginx SSE 透传配置正确 | 前端容器启动 | 检查 nginx.conf | proxy_buffering off |

## 11. 后端单元测试 (pytest)

| 用例 ID | 测试文件 | 模块 | 描述 | 运行命令 | 状态 |
|---------|----------|------|------|----------|------|
| TC-PY-01 | test_citations.py | 引用去重 | URL/chunk_id 去重 + 引用校验 + 参考列表渲染 | `pytest app/test/test_citations.py -v` | ✅ 20 passed |
| TC-PY-02 | test_events.py | 事件协议 | 事件注册表 + SSE 格式 + 配置加载 + 源码审计 | `pytest app/test/test_events.py -v` | ✅ 12 passed |
| TC-PY-03 | test_hitl.py | HITL | 澄清检测 + interrupt payload 校验 + 状态重建 | `pytest app/test/test_hitl.py -v` | ✅ 24 passed |
| TC-PY-04 | test_integration_audit.py | 集成审计 | HITL payload 一致性 + analyze 修复 + workflow 删除 + rollback 契约 | `pytest app/test/test_integration_audit.py -v` | ✅ 20 passed |
| TC-PY-05 | test_p1.py | P1 核心 | State reducer + 节点导入 + DDG 搜索 + Redis 缓存 + 拓扑 | `pytest app/test/test_p1.py -v` | ✅ 7 passed |
| TC-PY-06 | test_p2.py | P2 流式 | 流式协议 + 事件顺序 + 三不变式 | `pytest app/test/test_p2.py -v` | ✅ passed |
| TC-PY-07 | test_p3.py | P3 恢复 | 任务注册 + 409 并发 + 取消 + resume 语义 + 崩溃恢复 | `pytest app/test/test_p3.py -v` | ✅ passed |
| TC-PY-08 | test_p4.py | P4 HITL | clarify interrupt + plan approval + report review | `pytest app/test/test_p4.py -v` | ✅ passed |
| TC-PY-09 | test_p5.py | P5 记忆 | PostgresStore 单例 + schema 隔离 + memories API | `pytest app/test/test_p5.py -v` | ✅ passed |
| TC-PY-10 | test_p1_smoke.py | P1 冒烟 | 完整链路 + direct_answer 分支 | `pytest app/test/test_p1_smoke.py -v` | ⏭ skipped（需 langgraph + API key） |
| TC-PY-11 | test_resume.py | P3 恢复语义 | resume mode + payload 校验 + 崩溃恢复 | `pytest app/test/test_resume.py -v` | ✅ passed |
| TC-PY-12 | test_search_provider.py | DDG 搜索 | 搜索协议 + 失败降级 + 缓存命中 | `pytest app/test/test_search_provider.py -v` | ✅ passed |
| TC-PY-13 | test_state.py | State | reducer + 初始状态 + hitl config | `pytest app/test/test_state.py -v` | ✅ passed |
| TC-PY-14 | test_stream_events.py | 流式事件 | 注册表合规 + 终止事件语义 + 服务结构 | `pytest app/test/test_stream_events.py -v` | ✅ passed |

### 测试运行汇总

```bash
# conda llmdev 环境（Python 3.11 + 全量依赖）
cd D:\Code\LLMdev\deepresearch
conda run -n llmdev python -m pytest app/test/ -v --tb=short
# 结果：269 passed, 2 skipped, 0 failed, 0 errors in 6.70s

# base conda 环境（Python 3.14 + 仅基础包，conftest mock 第三方依赖）
python -m pytest app/test/ -v --tb=short
# 结果：267 passed, 4 skipped, 0 failed, 0 errors in 2.78s
```

**跳过原因**：
- `test_p1_smoke.py` (2 tests): 端到端冒烟测试，需要 `langgraph` 真实安装 + `DASHSCOPE_API_KEY` + PostgreSQL 可连接（llmdev 环境下 PG 未运行时也跳过）
- `test_p3.py` (2 tests, base only): 协程未 await 警告（不影响功能，仅资源清理提示）

### Mock 策略说明

测试环境通过 `conftest.py` 统一管理第三方依赖 mock：

| 依赖 | Mock 策略 |
|------|-----------|
| `langgraph` | StateGraph + START/END + InMemorySaver + Command + interrupt 具体实现 |
| `langchain_core` | HumanMessage/BaseMessage/AIMessage + tool/StructuredTool + Document |
| `langchain_community` | DashScopeEmbeddings + FAISS/Milvus + **ChatTongyi** |
| `langchain` | create_agent → MagicMock |
| `fastapi` | APIRouter + FastAPI + Depends + HTTPException + responses/middleware |
| `psycopg/redis/minio/pika` | 通配空 mock（通过 _WildcardMockFinder） |

### 编译图 Mock 说明

`conftest.py` 中的 `_CompiledGraph` 提供完整的 mock 编译图：
- `get_graph()`: 返回包含 `__start__`/`__end__` + 所有注册节点的图视图
- `invoke()`: 返回输入状态 + `final`/`intent` 默认值
- `astream()`: 异步生成器，yield updates 模式
- `get_state()`/`aget_state()`: 返回空快照（无 interrupt、无 next）
- `get_state_history()`: 返回空迭代器
- `update_state()`: 空操作

## 12. 修复历史

### 2026-09-03 测试修复

| 问题 | 根因 | 修复 |
|------|------|------|
| psycopg 连接超时 30s | Windows ProactorEventLoop 不兼容 | `store_client.py` + `app_main.py` 设置 SelectorEventLoopPolicy |
| 前端无对话界面 | `ChatView.vue` 缺少 `onUnmounted` 导入 | 补充 import + 清理 Vite HMR 缓存 |
| `/health/live` 404 | 健康检查路由缺失 | `health_router.py` 新增 `/health/live` 端点 |
| RabbitMQ 端口冲突 | `docker-compose.app.yml` 重复定义 | 移除重复的 rabbitmq 服务 |
| RollbackMenu 500 错误 | 虚拟线程 ID 查询后端 | `RollbackMenu.vue` 跳过虚拟 ID + `research_service` 异常处理 |
| ChatTongyi 不可调用 | mock 注册为 module 而非 class | `conftest.py` 新增 `langchain_community.chat_models.ChatTongyi` mock |
| create_agent 不可调用 | `langchain.agents` 未注册 mock | `conftest.py` 新增 `langchain.agents.create_agent` mock |
| StateGraph 不可调用 | `langgraph.graph` mock 无 StateGraph 类 | `conftest.py` 新增 `_StateGraph` + `_CompiledGraph` 完整实现 |
| test_p1_smoke MagicMock 返回值 | 编译图 mock 过于简单 | `_CompiledGraph.invoke` 返回合理 dict |
| test_p1_smoke 跳过条件 | API key 存在但 langgraph 未安装 | 增加 `find_spec('langgraph')` 检查 |
| llmdev 环境 psycopg_c.pq mock 覆盖 | `_WildcardMockFinder` 白名单含 psycopg 前缀，覆盖真实包子模块 | 从白名单移除 psycopg/langgraph/langchain，新增 `_is_real_installed` 用 PathFinder 检查 |
| llmdev 环境图节点 async/invoke 不兼容 | `langgraph 1.0.3` 异步节点不支持 `invoke`，需 `ainvoke` | `research_service.py` 的 `run`/`run_with_route` 改用 `await self._app.ainvoke()` |
| llmdev 环境 PostgresSaver NotImplementedError | 冒烟测试需真实 PG 连接 | 增加 PostgreSQL 可连接性检查到 skip 条件 |
