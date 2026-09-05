# 阶段三测试用例（可靠性与运维）

> 配套任务：R3.1 DLQ 死信队列与消费幂等 / R3.2 多实例取消广播 / R3.3 PDF 导出管线 / R3.4 配置热更新
> 优先级：P0 = 验收必过；P1 = 应尽量通过；P2 = 建议覆盖
> 通用规约：pika / Redis / Playwright / PG 全部 mock，不依赖真实中间件与浏览器。

## 1. 测试文件规划

| 测试文件 | 覆盖任务 |
| --- | --- |
| `app/test/test_chunk_dlq.py`（新增） | R3.1 |
| `app/test/test_task_cancel_broadcast.py`（新增） | R3.2 |
| `app/test/test_pdf_export.py`（新增） | R3.3 |
| `app/test/test_admin_config.py`（新增） | R3.4 |

## 2. T3.1 DLQ 死信队列与消费幂等（R3.1）

### T3.1-01 DLX 队列声明参数【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | mock pika channel（记录 declare 调用） |
| 步骤 | 执行 MQProducer.connect()（或消费者声明的等价函数） |
| 预期 | 业务队列声明 arguments 含 `x-dead-letter-exchange: "chunk-sync-dlx"` 与 `x-dead-letter-routing-key: "chunk.sync.dead"`；`chunk-sync-dlq` 队列与 `chunk-sync-dlx` 交换机被声明并绑定 |

### T3.1-02 首次失败 requeue 重试【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | mock `_process_message` 抛异常；mock channel 记录 ack/nack |
| 步骤 | 同一消息体（相同 body hash）投递第 1、2 次（`chunk_retry_limit=3`） |
| 预期 | 两次均为 `nack(requeue=True)`；无 ack；无 DLQ 路由 |

### T3.1-03 重试超限进 DLQ【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 同上，第 3 次投递同一消息体 |
| 步骤 | 第 3 次失败处理 |
| 预期 | `nack(requeue=False)`（消息路由 DLX → DLQ）；error 级日志含 chunk 标识；重试计数被清理（第 4 次同 body 投递从 1 重新计数） |

### T3.1-04 计数 key 为消息体而非 delivery_tag【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 同一消息体先后以 delivery_tag=5、delivery_tag=9 到达（requeue 后 tag 变化） |
| 步骤 | 两次失败处理 |
| 预期 | 第二次失败时重试计数为 2（而非各自从 1 开始） |

### T3.1-05 成功后计数清理【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | 消息体 A 失败 2 次后第 3 次成功 |
| 步骤 | 成功处理；随后 A 再次投递且失败 |
| 预期 | 成功时 ack 且计数清零；再次失败时从 1 重新计数 |

### T3.1-06 幂等跳过已索引 chunk【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | mock repo `get_chunk_status` 返回 "indexed"；mock 向量化调用计数 |
| 步骤 | `_process_message` 处理该消息 |
| 预期 | 向量化逻辑未被调用；外层正常 ack；info 日志「幂等跳过」 |

### T3.1-07 幂等不拦截待处理 chunk【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | `get_chunk_status` 返回 "pending" |
| 步骤 | 处理消息 |
| 预期 | 走完整向量化逻辑 |

### T3.1-08 状态回写失败视为处理失败【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | 向量化成功但状态回写 PG 抛异常 |
| 步骤 | 处理消息 |
| 预期 | 整体抛异常进入重试路径（不留「已向量化但状态 pending」的半态——由重试+幂等收敛） |

### T3.1-09 DLQ 重放脚本【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | mock pika basic_get 返回 2 条死信（含 x-death 头） |
| 步骤 | 运行 `scripts/replay_dlq.py` 的核心函数（import 调用，不走 __main__） |
| 预期 | 2 条消息以原 routing key 重发到业务交换机；重发成功后对 DLQ 消息 ack；--dry-run 模式不 publish |

## 3. T3.2 多实例取消广播（R3.2）

### T3.2-01 本地命中直接取消（行为不变）【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | registry 内注册一个未完成 task（asyncio task mock） |
| 步骤 | `await registry.cancel(thread_id)` |
| 预期 | task.cancel 被调用；不触发 redis publish（mock redis 断言 publish 未被调用）；返回 True |

### T3.2-02 本地未命中广播【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | registry 无该 thread 任务；mock redis（记录 publish 与 set 调用） |
| 步骤 | `await registry.cancel(thread_id)` |
| 预期 | `publish("task:cancel", json)` 被调用且 payload 含 thread_id 与 instance_id；`setex("cancel:{tid}", 300, "1")`；返回 True |

### T3.2-03 订阅方收到广播取消本地任务【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | registry A 注册了 thread_x 任务；构造广播消息 JSON |
| 步骤 | 调 `await registry._handle_cancel_broadcast(payload_bytes)` |
| 预期 | 本地任务被 cancel；info 日志含「远端取消」 |

### T3.2-04 广播消息对已完成任务无副作用【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | registry 内该 thread 的 task 已 done（mock done task） |
| 步骤 | 触发广播处理 |
| 预期 | 不抛异常；不调用 task.cancel |

### T3.2-05 畸形广播消息被忽略【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 广播消息为非法 JSON / 缺 thread_id |
| 步骤 | 触发处理 |
| 预期 | 静默忽略（无异常抛出） |

### T3.2-06 Redis 不可用降级【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | mock redis 的 publish 抛 ConnectionError；本地未命中 |
| 步骤 | `await registry.cancel(thread_id)` |
| 预期 | 返回 False；不抛异常；warning 日志 |

### T3.2-07 无 Redis 时纯本地语义【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | `init_task_registry(redis=None)`；本地未命中 |
| 步骤 | cancel |
| 预期 | 返回 False，无任何 redis 交互 |

### T3.2-08 订阅断线重连【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | mock pubsub.listen 第一轮抛异常后第二轮正常返回 |
| 步骤 | 跑 `_subscribe_loop`（timeout 控制） |
| 预期 | 退避后重新 subscribe；退避时长按 1s/2s 递增（sleep mock 断言） |

## 4. T3.3 PDF 导出管线（R3.3）

### T3.3-01 正常导出 PDF【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | mock async_playwright（返回假 browser/page，page.pdf 返回 b"%PDF-..."）；mock 会话消息含报告 |
| 步骤 | TestClient 调 `GET /threads/{id}/export/pdf` |
| 预期 | 200；Content-Type application/pdf；Content-Disposition 含 report_ 前缀；body 为 PDF 字节；page.pdf 参数含 A4 与页脚模板 |

### T3.3-02 Playwright 失败降级 Markdown【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | mock playwright launch 抛异常 |
| 步骤 | 同上调用 |
| 预期 | 200；Content-Type text/markdown；body 为报告原文；error 日志含「降级 Markdown」 |

### T3.3-03 无报告 404（行为不变）【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | thread 无 assistant 消息 |
| 步骤 | 同上调用 |
| 预期 | 404（与现状一致） |

### T3.3-04 HTML 模板渲染【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | 报告 Markdown 含 `# 标题`、代码块、表格 |
| 步骤 | 调用模板渲染函数检查输出 HTML |
| 预期 | 标题→h1、代码→pre/code、表格→table 标签；中文字体族、页码模板存在；正文 HTML 已转义（无原始注入） |

### T3.3-05 weasyprint 引用清零【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | — |
| 步骤 | `grep -ri weasyprint app/ requirements.txt` |
| 预期 | 无匹配（或仅在文档中出现） |

## 5. T3.4 配置热更新（R3.4）

### T3.4-01 reload 成功原子生效【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 临时写 config.json `max_iterations=7`（monkeypatch 路径） |
| 步骤 | `POST /api/v1/admin/config/reload` 后读 `get_business_settings()` |
| 预期 | 响应 `reloaded=true`；`max_iterations==7`；applied_fields 含 max_iterations |

### T3.4-02 非法配置 422 拒载【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 临时写 config.json 含类型错误（如 `max_iterations: "abc"`） |
| 步骤 | reload |
| 预期 | 422；detail 含字段级错误；`get_business_settings()` 保持旧值（未被污染） |

### T3.4-03 JSON 解析失败 422【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 临时写非法 JSON 文本 |
| 步骤 | reload |
| 预期 | 422；内存配置不变 |

### T3.4-04 需重启字段提示【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 修改 `model` 字段（需重启项）后 reload |
| 步骤 | 检查响应 |
| 预期 | `restart_required_fields` 含 "model"；说明热更与重启边界在响应中显式可见 |

### T3.4-05 GET /config 脱敏【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | BusinessSettings 含任意密钥类字段时（执行时以实际字段为准） |
| 步骤 | `GET /api/v1/admin/config` |
| 预期 | 响应不含 api_key/dsn/password 类字段（有则加入排除列表后重测通过） |

### T3.4-06 ADMIN_TOKEN 鉴权【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | `.env` 设 ADMIN_TOKEN=secret；无头请求与错误 token 请求 |
| 步骤 | 调 reload |
| 预期 | 无头/错 token → 401；正确 `X-Admin-Token: secret` → 200；token 为空时放行 |

### T3.4-07 reload 回调失效搜索链【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | R1.1 的 `_PROVIDER_CHAIN` 已构建（非 None）；注册了失效回调 |
| 步骤 | reload 后检查 `tools._PROVIDER_CHAIN` |
| 预期 | 重置为 None（下次使用按新配置重建） |

## 6. 集成与浏览器自动化验证（阶段三收尾统一执行）

> 以下用例通过 Playwright 浏览器自动化 + API 级自动化执行，脚本存放于 `agent_front/e2e/phase3_reliability.spec.ts`。
> 前置：后端服务运行在 `http://127.0.0.1:8000`，前端运行在 `http://localhost:5173`（或 8080），中间件（PG/Redis/Milvus/RabbitMQ）已启动。
> 选择器参考：`.export-pdf-btn`（导出按钮）、`.upload-dropzone`（上传区）、`.upload-task-list`（上传任务列表）、`.stats-cards`（统计卡片）、`.document-table`（文档表格）。
> 说明：M3-01/M3-02/M3-03 涉及多实例与 MQ 管理台，采用 API 级自动化（`page.request`）验证；M3-04~M3-06 以浏览器 UI 自动化为主。

| 编号 | 场景 | 自动化步骤 | 预期 |
| --- | --- | --- | --- |
| M3-01 | 双实例取消广播 | 1. 通过 `page.request` 向实例 A（8000）发起研究请求（POST `/api/v1/research/threads`，body 含 query）<br>2. 等待流开始后，通过 `page.request` 向实例 B（8001）发送 cancel（POST `/api/v1/research/threads/{id}/cancel`）<br>3. 轮询实例 A 的 `GET /api/v1/research/threads/{id}/state`<br>4. 断言 state.status 最终为 `cancelled`<br>5. 检查 PG 无 running 残留标记（通过 `page.request` 调 `GET /api/v1/research/threads/{id}/state` 确认） | A 实例日志出现「远端取消命中」；流终止；PG 无 running 残留标记 |
| M3-02 | DLQ 实投 | 1. 导航至 `/knowledge` 页面<br>2. 通过 `page.setInputFiles` 上传一个构造必失败的文档（损坏 docx）<br>3. 等待 `.upload-task-list` 显示失败状态<br>4. 通过 `page.request` 调 RabbitMQ 管理 API（`GET /api/queues/%2F/chunk-sync-dlq`）检查死信队列<br>5. 断言队列中有消息<br>6. 通过 `page.request` 调用 `scripts/replay_dlq.py --dry-run` 等效 API（或直接 `child_process` 调脚本）<br>7. 断言 dry-run 输出含该消息 | 消息重试 3 次后出现在 `chunk-sync-dlq`（管理台 15672 可见）；error 日志告警；`python scripts/replay_dlq.py --dry-run` 列出该消息 |
| M3-03 | 幂等实投 | 1. 导航至 `/knowledge` 页面，上传正常文档并等待 `indexed`<br>2. 通过 `page.request` 获取 outbox 消息 ID<br>3. 手动对同一 outbox 消息触发两次补偿重投（通过后端 API 或 MQ 重投）<br>4. 等待处理完成后检查 `.document-table` 中该文档的 `indexed_chunks` 数量<br>5. 断言 `indexed_chunks` 未翻倍（幂等生效） | 向量化只执行一次（第二次日志「幂等跳过」）；Milvus 无重复向量 |
| M3-04 | PDF 导出实机 | 1. 导航至 `/chat` 页面，完成一次研究报告（等待 `.message-item.assistant` 渲染完整报告）<br>2. 点击 `.export-pdf-btn`（「📥 导出」）按钮<br>3. 等待下载完成（`page.waitForEvent('download')`）<br>4. 断言下载文件名以 `report_` 开头且扩展名为 `.pdf`<br>5. 检查文件大小 > 0<br>6. 通过 `page.route` mock Playwright launch 失败后再次点击导出<br>7. 断言降级返回 `.md` 文件（检查下载文件扩展名） | 下载的 PDF 中文正常、代码块/表格排版正确、页脚含页码；杀掉 Chromium 后再导出 → 降级返回 .md 文件 |
| M3-05 | 配置热更实机 | 1. 通过 `page.request` 修改 `config.json` 中 `max_iterations` 值<br>2. 调 `POST /api/v1/admin/config/reload`（通过 `page.request`）<br>3. 断言响应 `reloaded=true` 且 `applied_fields` 含 `max_iterations`<br>4. 导航至 `/chat` 页面发起新会话，验证新配置生效<br>5. 通过 `page.request` 修改 `model` 字段后 reload<br>6. 断言响应 `restart_required_fields` 含 `model` | 与 3.4 生效边界表一致 |
| M3-06 | 文档管道回归 | 1. 导航至 `/knowledge` 页面<br>2. 通过 `.upload-dropzone` 上传一份正常 MD 文档<br>3. 等待 `.upload-task-list` 显示上传成功（毫秒级返回）<br>4. 轮询 `.stats-cards` 直到 `pending_chunks` 为 0 且 `indexed` 数量增加<br>5. 检查 `.document-table` 中该文档的 `vector_state` 为 `indexed`<br>6. 导航至 `/chat` 页面提问该文档内容，验证检索命中 | 上传毫秒级返回；向量化状态最终 indexed；上传→入库→消费全链路无回归 |

## 7. 回归范围

- 后端：`python -m pytest app/test -q` 全量，重点关注 `test_p3.py`（取消/恢复）、document 相关测试、`test_p1_smoke.py`
- 涉及 R3.1 的消费链路改动后，确认 `docker compose -f docker-compose.middleware.yml` 中 RabbitMQ 正常，上传→向量化 E2E 无回归（M3-06）
- Docker 镜像改动（R3.3）后执行 `docker build` 确认构建通过
