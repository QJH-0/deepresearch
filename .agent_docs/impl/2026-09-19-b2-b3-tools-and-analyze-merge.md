# B2 + B3 执行记录：deep_dive 接入工具 · reflect 并入 analyze

- 日期：2026-09-19
- 阶段：方案 B 第 2、3 步（智能层）
- 前置：B1（`retrieve_grader` 自适应重检）

## 前置澄清（改变了 B2 的前提）

项目 `AGENTS.md` 写着「节点只直调函数，不经过 agent tool-calling（所有 agent 的
tools 必须为空，有测试锁住）」。我一度把它当成用户要求，建议改用「节点内直调抓取」
来绕开。**用户澄清：他从未提过这条，他的原始诉求恰恰相反 ——
「每个节点都只是单纯的 LLM 调用，没有发挥 agent 的特点」。**

于是 B2 按原计划真正引入 tool-calling。

## B2：`deep_dive` 接入 `fetch_url`

### 为什么只给 `deep_dive` 加工具

按 Anthropic 的判据（任务路径可预测 → workflow 更优）：

| 入口 | 方式 | 理由 |
| --- | --- | --- |
| `web_search_records` / `search_knowledge_base_records` | 节点**直调** | 「已知必须检索」路径可预测，workflow 更可控更便宜 |
| `fetch_url_tool` | **绑给 `deep_dive`** | 「哪些证据可疑、要不要读原文」运行时才知道，属 agent 适用场景 |

### 关键技术约束（实测）：带工具必须用 `ToolStrategy`

`ProviderStrategy` 的原生 json_schema 与 tool_calls **不兼容**：

```
StructuredOutputValidationError: Failed to parse structured output for tool 'Out':
Native structured output expected valid JSON for Out, but parsing failed:
Expecting value: line 1 column 1 (char 0)
```

成因：模型先返回工具调用（`content` 为空），框架却拿空 content 去解析 JSON。
`ToolStrategy` 把 schema 作为一次工具调用下发，与业务工具可共存。

**代价**：约束强度略低于原生 json_schema（模型走工具调用通道提交结果）。
无工具的节点仍走 `ProviderStrategy`，不受影响。

### `fetch_url` 的实现要点

- `httpx` 抓取 + **标准库 `HTMLParser`** 抽正文：`bs4` 只是传递依赖、未在
  `requirements.txt` 声明，不为一个工具引入新依赖
- 丢弃 `script`/`style`/`nav`/`header`/`footer` 等非正文区块
- **SSRF 防护**：解析后的 IP 必须是公网，拒绝回环/私有/链路本地/保留/多播。
  只看域名白名单不够 —— 攻击者可用指向 `169.254.169.254` 的域名绕过
- **失败返回带前缀的说明字符串而非抛异常**：工具输出直接进 agent 上下文，
  失败原因本身就是它决定下一步的依据

### 验证

| 项 | 结果 |
| --- | --- |
| 工具接线 | `evidence_judge` 的图含 `tools` 节点；`analyst` / `retrieval_grader` 不含 |
| **端到端调用** | 明确要求抓取时，模型调用 `fetch_url` 取回 `example.com` 的正文首句 "Example Domain" 并写入结构化 `summary` |
| 流式路径 | `_invoke_structured_agent` 的 `astream` + ToolStrategy 正常返回 `structured_response` |
| 单测 | 新增 `test_fetch_url.py` 14 例（SSRF 6 + HTML 抽取 2 + 行为 5 + 工具描述 1） |

**观察到的现象（需更多样本再判断）**：在真实评测运行中，`fetch_url` 调用 **0 次**。
工具可用、链路正常，是模型判断现有证据无需读原文。提示词目前写的是
「只对真正存疑的证据抓取」，可能偏保守。**未据此调提示词** —— n=1 不足以支撑调参。

### 顺带修掉一个空断言

`test_no_agent_is_built_with_tools` 只 patch 了 `build_agent`（自由文本节点
direct_answer / write / clarify），而**真正可能带工具的结构化节点走
`build_structured_agent`，从未被检查**。于是 AGENTS.md 声称「有测试锁住」的
不变量实际无人守护。已同时 patch 两个入口、断言结构化节点被全部覆盖，
并改为断言「只有 deep_dive 带工具」。

## B3：`reflect` 并入 `analyze`

### 合并理由

缺口与补检词本就出自同一次判断。分两次调用会让模型先说出缺口、再被要求
根据自己刚写的缺口出词 —— **多一次 LLM 调用，还多一次信息损失**。

### 改动

- `AnalysisDraft` 新增 `gap_queries`；analyze 一次产出结论映射 + 缺口检索词
- analyze 同时产出 `next_action`（含迭代上限判定）与轮次推进
  - 上限是安全闸，**不能被模型判断绕过**
  - HITL 的 `user_supply` / `skip` 也会把 `needs_more_research` 置假，
    同样由本节点统一收敛
- 路由 `should_continue_research` → `route_after_analyze`，只读 `next_action`
- 删除 reflect 节点 / agent / 提示词 / `ReflectionDraft` / config 里的 reflect 型号
- **原 reflect 提示词里的「历史研究笔记」上下文移到 analyze 提示词** —— 不能丢

节点数 **11 → 10**（B1 加 grader、B3 并 reflect）。

### 顺带修一个自己引入的前向引用

`AnalysisDraft` 原本定义在 `SupplementaryQuery` **之前**，给它加
`gap_queries: list[SupplementaryQuery]` 会 `NameError`（6 个测试文件收集期即失败）。
已把 `SupplementaryQuery` 上移到 `AnalysisDraft` 之前。

## 验证

| 项 | 结果 |
| --- | --- |
| 后端全量 | **636 passed, 2 skipped** |
| 回归专项 | **116 passed, 7 deselected**（与基线一致） |
| 前端 | **6 文件 71 passed**（与基线一致） |
| **人为漂移验证** | 去掉 analyze 的迭代上限闸（无条件推进轮次）→ `test_analyze_stops_at_iteration_cap` **FAILED**；还原后 15 passed |
| 拓扑锁定 | 边集合断言更新为 `analyze → {web_search, local_rag, write}`，reflect 相关边已移除 |

## 待验证 / 遗留

- **B4 未做**：查询策略对齐 "start wide, then narrow"（`_derive_direct_search_queries` 产出长 query）
- **`fetch_url` 的实际使用率**需更多样本观察；若长期为 0，应重新审视提示词或考虑去掉工具
- **成本**：B2/B3 后的端到端评测仍在跑，token 与耗时影响待读
- **原计划「消除检索节点里信息增量为零的 LLM 重写」仍未做**：需先确认
  `rejected_source_ids` / `gaps` / `supports_questions` 的消费方
