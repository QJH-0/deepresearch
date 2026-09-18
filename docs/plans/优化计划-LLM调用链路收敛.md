# LLM 调用链路收敛计划（6 个优化点）

> 来源：2026-09-17 架构调研后确认的 6 个可改点
> 状态：**待确认**（按 AGENTS.md「大改动前置输出模板」，确认后方可执行）
> 编写日期：2026-09-17

---

## 一、目标（Goal）

收敛 LLM 调用链路：消灭重复调用与静默降级，把结构化输出统一到官方路径，去掉无收益的思考开销与全局环境污染。

**完成标准**：6 个点全部落地；`pytest app/test` 全绿；结构化约束强度**不弱于**当前；单轮研究耗时下降（点 1 实测后确认）。

**验证方式**：每阶段独立跑相关测试子集 + 真实调用一次决策节点链路。

---

## 二、关键前提核实结果（本次实测，推翻了此前的结论）

此前文档记载「`create_agent(response_format=...)` 报 InvalidParameter，必须绕开官方路径」。**该结论已过时**——它成立的前提是 `ChatTongyi` 原生通道，而生产代码早已切到 `ChatOpenAI` 兼容通道。本次在兼容通道上重测：

| 路径 | 实测结果 | 请求体实际内容 |
| --- | --- | --- |
| `create_agent + ProviderStrategy(schema)` | **成功**，2.8s | `response_format=json_schema`（**无 strict**）+ `bind_tools` |
| `create_agent + 裸 schema` | **成功**，1.7s | `tools` + `tool_choice=required`（即 ToolStrategy） |
| 当前自建 `llm.bind(...)` | 在用 | `response_format=json_schema` + **`strict: true`** |

三条源码事实（langchain 1.0.x / langchain_openai 1.6.0）：

1. **`_supports_provider_strategy` 是硬编码白名单**：只认 `grok` / `gpt-5` / `gpt-4.1` / `gpt-oss` / `o3-pro` / `o3-mini`（`factory.py:352-372`）。`qwen` 不在内，所以**裸 schema 永远退化成 ToolStrategy**，不会自动选 ProviderStrategy。
2. **`ProviderStrategy.to_model_kwargs()` 不含 strict**（`structured_output.py:263-274`），只产出 `{"type":"json_schema","json_schema":{"name":...,"schema":...}}`。`strict` 参数是 langchain **≥1.2** 才加入的，当前装的是 1.0.x。
3. **`ProviderStrategy` 的绑定实现仍是 `model.bind_tools(final_tools, strict=True, **kwargs)`**（`factory.py:1011-1019`），并非纯粹的 response_format。

另：再次确认 `astream` 下 `reasoning_len=0`，即兼容通道确实不透出思考内容（与既有记录一致）。

---

## 三、阻塞问题（2 个，需你决策）

### 阻塞问题 1：回官方路径会丢掉 `strict` 约束（点 3 的核心取舍）

`strict: true` 是 provider 级强制，也是当初借鉴 ai-ppt 的核心价值。当前自建写法有它，而 langchain 1.0.x 的 `ProviderStrategy` **没有**。

| 方案 | 做法 | 收益 | 代价 |
| --- | --- | --- | --- |
| **A（推荐）** | 先升 langchain 至 **≥1.2**（最新 1.4.1 可用），再用 `ProviderStrategy(schema, strict=True)` | 官方路径 + 保留 strict，可删掉自建的 response_format 构造 | 依赖升级，需验证全量测试 |
| B | 不升级，直接用 1.0.x 的 `ProviderStrategy` | 立刻回官方 | **约束变弱**（丢 strict），与优化目标相悖 |
| C | 维持自建，等将来升级再迁 | 零风险 | 自建代码继续存在（约 40 行） |

**默认建议 A**：升级到 1.2+ 后 `ProviderStrategy` 与当前自建等价，才能真正"回官方且不退化"。升级范围仅限 langchain（langchain-core 已是 1.6.2，无需动）。

### 阻塞问题 2：点 1 关掉 thinking 是否影响输出质量

`thinking_nodes` 目前给 `write` / `deep_dive` / `analyze` 开思考，但兼容通道不透出 `reasoning_content`——**模型在思考、用户看不到、延迟照付**。

是否能关取决于质量：**思考可能提升长文与深度分析的质量**，不能只看耗时就关。必须先做开/关对比。

**默认建议**：先跑对比（同 3 个问题 × 开/关，比对耗时与输出），质量无可见差异再关；若质量有差异，改为保留 `deep_dive`/`analyze`、只关 `write`（write 是模板化长文，思考收益最小）。

---

## 四、假设（Assumptions）

| 维度 | 假设 |
| --- | --- |
| 环境 | 全部用 conda `llmdev`；依赖升级只动 `langchain`，不动 `langchain-core` / `langchain-openai` |
| 兼容性 | langchain 1.2+ 的 `create_agent` / `ProviderStrategy` API 与 1.0.x 兼容；若不兼容则回退方案 C |
| 降级语义 | 三个检索节点（web_search / local_rag / deep_dive）改结构化后，失败时**显式抛错**而非静默 fallback；需确认「空结果继续跑」是否可接受（当前 fallback 就是这个语义，行为不变，只是失败要留痕） |
| 范围 | 不改 State 契约、不改 SSE 事件字段、不改前端 |
| 测试 | 各阶段新增用例；Fake/打桩沿用现有方式（不引入统一 Fake LLM，理由见既有记录） |

---

## 五、计划（Plan）

### 阶段 0 — 升级可行性验证（点 3 的前置）

| 项 | 内容 |
| --- | --- |
| 0.1 | 在 llmdev 中把 langchain 升到 1.2+（先试 1.4.1），跑 `pytest app/test --ignore=test_fix_regressions.py` |
| 0.2 | 验证 `ProviderStrategy(schema, strict=True)` 可用，且请求体带 `strict: true` |
| 0.3 | 若失败或破坏面过大 → 回退方案 C，点 3 改为「维持自建」，只做点 1/2/4/5 |

**验收**：0.1 全绿 且 0.2 请求体含 strict。
**回滚**：`pip install langchain==1.0.x` 即可。

---

### 阶段 1 — 零风险清理（点 5 + 点 1）

| 项 | 点 | 改动 |
| --- | --- | --- |
| 1.1 删除全局环境污染 | 5 | `models.py:_build_llm` 删掉 `os.environ["DASHSCOPE_API_KEY"] = api_key`；api_key 一律显式传参 |
| 1.2 简化测试隔离 | 5 | `conftest.py` 里为隔离 `DASHSCOPE_API_KEY` 加的 autouse fixture 随之评估是否可删 |
| 1.3 thinking 开/关实测 | 1 | 同 3 个问题跑开/关两遍，记录耗时与输出，形成结论 |
| 1.4 按结论调整配置 | 1 | 质量无差异 → `thinking_nodes` 置空；有差异 → 只保留 `deep_dive`/`analyze` |

**验收**：1.1 后全量测试仍绿（尤其是 `test_events.py` 里读 `.env` 的用例）；1.4 有实测数据支撑。

---

### 阶段 2 — 消灭重复调用（点 2 的一半）

| 项 | 改动文件 | 要点 |
| --- | --- | --- |
| 2.1 去掉兜底重复调用 | `nodes/_parsing.py:153-161` | 流式无内容时**不再** `ainvoke` 第二次；直接按失败处理 |

当前逻辑：流式拿到空 → 再 `ainvoke` 一次拿内容，等于同一请求付费两次。

**验收**：单测断言「流式为空时不发生第二次调用」。

---

### 阶段 3 — 结构化输出回官方路径（点 3，依赖阶段 0）

| 项 | 改动文件 | 要点 |
| --- | --- | --- |
| 3.1 改用官方策略 | `models.py` | `build_structured_agent` 改为 `create_agent(model=llm, tools=[], system_prompt=..., response_format=ProviderStrategy(schema, strict=True))` |
| 3.2 统一调用范式 | `nodes/_parsing.py` | 结构化节点改走 `agent.astream({"messages":[...]}, stream_mode="messages")`，与普通节点一致 |
| 3.3 保留 strict 断言 | `models.py` / 测试 | 新增测试：请求体必须含 `strict: true`（防止依赖升级后悄悄退化） |
| 3.4 型号白名单保留 | `models.py` | `JSON_SCHEMA_MODELS` 保留——它提供「配错型号启动即失败」，官方路径没有这个保护 |

**验收**：四个决策节点真实调用均走结构化、无降级告警；`test_structured_output.py` 全绿并新增 strict 断言。

**回滚**：`build_structured_agent` 是单点，改回 bind 即可。

---

### 阶段 4 — 三个检索节点结构化（点 4）+ 静默 fallback 显式化（点 2 的另一半）

| 项 | 改动文件 | 要点 |
| --- | --- | --- |
| 4.1 定义证据 schema | `output_schemas.py` | 为 `web_search` / `local_rag` / `deep_dive` 定义 Pydantic 模型（deep_dive 的 `evidence_pool` / `source_index` / `audit_flags` 嵌套最深，重点） |
| 4.2 三节点改造 | 三个 node 文件 | 改走 `_invoke_structured_agent` |
| 4.3 删除静默 fallback | `nodes/_parsing.py` | 结构化失败一律抛 `StructuredOutputError`；节点内决定是否降级（与 intent 的「回退规则引擎并留痕」同款） |
| 4.4 清理死代码 | `_parsing.py` | 删除 `_extract_json_block` / `_load_json`；`collect_tool_calls` 在 tools 恒为空的前提下评估是否保留 |

**验收**：三节点真实调用各一次，输出与改造前结构等价；测试覆盖「结构化失败 → 显式错误」。

**风险**：`deep_dive` 的证据结构复杂，schema 可能过严导致模型输出被拒。 mitigation：schema 只约束**结构必需字段**，其余字段给默认值；若实测不稳定，该节点回退自由文本但不回退「静默 fallback」。

---

## 六、执行顺序与依赖

```
阶段 0（升级验证）── 不通过则点 3 改走方案 C
   │
阶段 1（点 5 + 点 1）── 无依赖，可立即开始
   │
阶段 2（点 2 去掉重复调用）── 独立
   │
阶段 3（点 3 回官方）── 依赖阶段 0
   │
阶段 4（点 4 + 点 2 fallback）── 依赖阶段 3（schema 机制定型后再推广）
```

建议实际顺序：**阶段 1 → 阶段 2 →（阶段 0）→ 阶段 3 → 阶段 4**。即先把零风险项做完，再动结构化。

---

## 七、风险与回滚

| 风险 | 缓解 |
| --- | --- |
| langchain 升级破坏现有行为 | 阶段 0 先跑全量测试；不通过就回退方案 C，不影响其他阶段 |
| 回官方后丢失 strict | 阶段 3.3 加请求体断言锁住 |
| 关 thinking 导致质量下降 | 阶段 1.3 必须先实测；有差异就只关 write |
| deep_dive schema 过严 | 只约束必需字段；不稳定则保留文本解析但去掉静默 fallback |
| 点 4 改动面较大 | 放在最后，前三阶段已验证的机制稳定后再动 |

**回滚**：阶段 1/2 为代码级改动，`git revert` 即可；阶段 3 有单点开关；阶段 0 回退只需重装依赖版本。

---

## 八、执行记录（2026-09-17 ~ 09-18，6 个点全部完成）

用户决策：点 3 走**方案 A**；先关闭 thinking；授权 git 操作，按功能提交。共 7 个提交。

| 阶段 | 点 | 结果 | 提交 |
| --- | --- | --- | --- |
| — | 遗留 | 修复 2 个因生产代码演进而过期的用例 | `8afacb1` |
| — | 遗留 | 补回 AGENTS 环境说明、重写借鉴点文档、新增面试专题 | `098fe6a` |
| 1 | 点 1 | 关闭深度思考（实测耗时降 44%~64%，质量无下降） | `d6b4251` |
| 1 | 点 5 | 移除模型工厂对全局环境的写入 | `6b312f6` |
| 2 | 点 2 | 流式无内容时不再补一次完整调用 | `10e630e` |
| 0 | 点 3 前提 | langchain 升到 1.2（声明 `>=1.2`） | `3a902ed` |
| 3 | 点 3 | 结构化输出回归官方 `create_agent + ProviderStrategy` | `324f29a` |
| 4 | 点 4 + 点 2 | 三个检索节点结构化，删除 JSON 解析路径 | `000dfe4` |

### 与原计划的偏差

1. **升级目标定为 1.2.0 而非最新的 1.4.1**。dry-run 显示 1.4.1 会连带升级 langgraph 1.0.3 → 1.2.11、checkpoint 3.0.1 → 4.2.0，影响面远超预期；1.2.0 只动 langchain 本身，已满足 `ProviderStrategy(strict=...)` 的要求。
2. **点 2 的改动对象在阶段 4 后被整体删除**。点 2 优化的是 `_invoke_json_agent`，而阶段 4 把最后三个使用者改成结构化后，该函数（连同 `_load_json` / `_extract_json_block`）已无任何调用者 —— 重复调用的隐患随之消失，比优化它更彻底。write 节点早已自实现 astream 循环，只是仍保留着导入。
3. **阶段 4 顺带清理了既有死代码**：`collect_tool_calls`（tools 恒为空，无使用者）及四个节点中的未使用导入。

### 执行中发现的问题（未处理）

| 问题 | 说明 |
| --- | --- |
| `write.py` 剥离 ```json 围栏 | write 产出的是 Markdown 报告，却仍有 `re.sub(r"^\`\`\`json\s*", "", content)`。历史遗留（早期 write 可能产出 JSON），本轮未处理 |
| `web_search` 的 `domain` 字段语义漂移 | 实测中模型把 `domain` 填成了子问题名而非域名。该字段不参与下游判定，仅影响展示，未收紧 schema |
| 真实端到端研究仍未跑通 | 需 Milvus / RabbitMQ / 可访问的搜索引擎；本轮所有真实调用都是节点级验证 |

### 最终验证

| 范围 | 结果 |
| --- | --- |
| 后端全量（排除需 MQ 的挂起文件） | **536 passed, 2 skipped** |
| 回归专项 | **116 passed, 7 deselected** |
| 真实调用：4 个决策节点 | 全部走结构化无降级（intent 2.6s / plan 36.7s / analyze 14.8s / reflect 7.5s） |
| 真实调用：3 个检索节点 | 全部产出合法 schema（web_search 4.8s / local_rag 7.2s / deep_dive 15.3s） |
