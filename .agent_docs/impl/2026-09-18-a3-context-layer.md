# A3 执行记录：上下文层（note-taking + 证据预算）

- 日期：2026-09-18
- 阶段：方案 A 第 3 步（收尾）
- 计划来源：`docs/plans/优化开发计划-多智能体架构升级.md` §4 阶段 A3（含实施中的修正说明）
- 前置：A1、A2 已完成

## 顺序修正（实施中发现，已报告用户并获准）

原计划顺序是「A3-1 分层取用 → A3-2 compaction → A3-3 note-taking → A3-4 预算」。
A1 落地后回看，发现原排序的前提已变：

- **上下文膨胀的主因已被 A1 消除**（原本每轮证据列表近似翻倍），预算/裁剪机制价值下降
- 无 Golden Set 评估集 → 阈值收紧无法验证是否伤及报告质量，放松则几乎不触发
- **A3-3 note-taking 是 A3 里唯一的新增能力**，且不依赖评估集

修正后：**note-taking 优先，预算降级为「防病态膨胀的安全阀」**，compaction 并入预算模块。

## 改动清单

### 新增 `app/mult_agents/nodes/_context.py`

| 函数 | 职责 |
| --- | --- |
| `select_evidence` | 按 `reliability_score`（优先）/ `relevance_score` 取 top-k，受字符预算约束 |
| `compact_evidence` | 截断长文本字段（snippet/notes/reliability_reason），**保留全部结构化字段** |
| `evidence_for_prompt` | 节点入口：`evidence_pool` 优先，为空时回落原始证据 |
| `raw_evidence_for_prompt` | 裁判节点入口：读指定来源的**原始**证据（deep_dive 不能用 pool——那是它自己的输出） |
| `build_research_note` | 从 findings / missing_gaps / audit_flags 确定性汇编本轮笔记 |
| `render_research_notes` | 把历史笔记渲染成紧凑文本，供补搜规划避免重复检索 |

设计要点：

- **返回顺序保持原始相对顺序**，不按分数重排——prompt 里证据的排列影响引用编号可读性，
  重排会让引用角标与正文叙述脱节
- **装不下的大项跳过，让位给能装下的小项**；若一条都装不下仍保留最高分那条，
  返回空列表会让下游误判为「没有检索到证据」
- **只截断不丢字段**：下游依赖 `source_id` / `snippet` 做论断归属，丢字段会破坏引用溯源
- **笔记用确定性汇编而非再调一次 LLM**：输入已是结构化结论，让模型转述一遍只增加成本与不确定性

### 配置（三处同步，单一维护源在 config.json）

`context_evidence_limit: 40`、`context_evidence_budget_chars: 60000`
→ `config.json` / `BusinessSettings` / `AppConfig`（含 `from_file` 映射）

**不写入 State**：预算属配置项，写进 State 会与 `config.json` 形成第二个事实源。

### 接线

| 文件 | 改动 |
| --- | --- |
| `nodes/analyze.py` | 证据池改用 `evidence_for_prompt`；新增 `research_notes` 返回（只返回本轮一条）；`reflect` 的 prompt 加入历史笔记 |
| `nodes/deep_dive.py` | web/local 证据改用 `raw_evidence_for_prompt` |
| `nodes/_fallbacks.py` | 报告附录新增「### 研究过程」章节 |
| `state.py` | 新增 `research_notes: Annotated[list[dict], operator.add]`（累积型，节点只返回增量） |

### 刻意未做

- **`write` 节点不接预算**：其输入 `findings`/`source_index`/`audit_flags` 在 A1 之后已是
  「当前值」语义（每轮覆盖），不再无界增长；且这些是报告正文与引用的直接来源，
  裁剪会静默截断报告内容
- **`deep_dive` 裁剪不会丢引用**：被裁掉的证据仍由节点内的确定性回填
  （`deep_dive.py:59-78`）进入证据池，只是拿先验分而非 LLM 裁判分

## 验证证据

| 项 | 结果 |
| --- | --- |
| 新增测试文件 | `app/test/test_context_budget.py`（24 用例） |
| 后端全量（排除回归文件） | **570 passed, 2 skipped**（A1+A2 后为 546 → +24） |
| 回归专项（deselect 3 个需 MQ 的类） | **116 passed, 7 deselected**（与基线一致） |
| 前端 | **6 文件 71 passed**（与基线一致） |
| **人为漂移验证** | 把 `analyze` 的证据池改回全量 `json.dumps(state.get("evidence_pool"))` → `test_prompt_evidence_is_bounded` **FAILED**；还原后 24 passed |
| 接线验证 | `test_prompt_evidence_is_bounded` 构造 100 条证据 + 配置阈值 5，断言 prompt 中恰好 5 条且含最高分项——验证的是**接线**而非纯函数 |

## 遗留

- 阈值默认值（40 条 / 60000 字符）是**安全阀**取值，非调优结果。建立 Golden Set 评估集后
  才应评估是否收紧
- 真实端到端研报仍未跑通（需 Milvus + 可访问搜索引擎），预算与笔记的实际效果需实机确认
- 检查点：按用户决策清理开发环境，不写迁移逻辑
