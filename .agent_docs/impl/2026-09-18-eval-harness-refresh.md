# 评估设施修复：规则型指标层 + token 统计静默失败

- 日期：2026-09-18
- 定位：B 阶段（智能层）的前置条件——没有可用的评估，无法判断 B 的升级是否值得
- 关联计划：`docs/plans/优化开发计划-多智能体架构升级.md` §4 阶段 B「前置条件」

## 背景：先摸清既有评估设施

| 资产 | 内容 | 状态 |
| --- | --- | --- |
| `app/test/eval_metrics.py` | 端到端评测脚本：30 条多智能体题 + 20 条直答题，7 项指标（LLM-as-Judge 为主） | 需真实模型与中间件；**发现两处缺陷** |
| `scripts/run_rag_eval.py` | RAG 层评测：12 条题，召回/精确/要点覆盖/延迟，模块消融 | 需 Milvus |
| 测试集 | 已存在（多智能体 30 条含 `key_points`） | 缺「期望引用的来源」字段 |

结论：**Golden Set 已存在，缺的不是题而是「能判定 A/B 改动是否有效」的指标与可信的统计**。
两个脚本都需要真实中间件，本环境无法端到端运行，因此本次只做**可离线验证**的部分。

## 缺陷 1：token 统计恒为 0（静默失败）

**实测证据**：

```
type          : StructuredAgent
has _generate : False
runnable type : CompiledStateGraph
```

`TokenAccumulator.attach()` 的挂载条件是 `hasattr(agent, "_generate")`，
而结构化节点是 `StructuredAgent`（内含 `create_agent` 编译出的图），**没有 `_generate`**
→ 挂载循环一次都没进入 → `total_tokens` 恒为 0，且不报错。

**后果**：报告里「Token 消耗降低比例」是假数据（恒 0%）。这是典型的
「降级分支条件不可达」——降级逻辑存在，但触发条件永远不成立。

**修法**：改为 LangChain 回调（`BaseCallbackHandler.on_llm_end`），
经 `app.invoke(state, {"callbacks": [acc]})` 注入，覆盖图内所有嵌套 LLM 调用。

- usage 解析兼容两种落点：`llm_output["token_usage"]` 与 `message.usage_metadata`
- **解析不到时计数并告警**（`warn_if_unparsed`），避免再次静默归零

## 缺陷 2：规则型指标全部内联在脚本里，无法单测

原脚本把引用校验、要点覆盖等规则逻辑写在 572 行的过程式代码中，
既无法单测，也无法被其他工具复用。

**修法**：抽出 `app/mult_agents/eval_metrics.py`（纯函数、零外部依赖），新增四个指标：

| 指标 | 回答的问题 | 为何需要 |
| --- | --- | --- |
| `evidence_duplication_rate` | 同一 `source_id` 是否重复堆积 | **A1 的哨兵**——reducer 语义若再写错，重复率会立刻上升 |
| `retrieval_rounds` | 外层研究轮数、每轮检索查询数 | **判定 B1**——自适应检索生效后 `queries_per_round` 应上升 |
| `citation_legality` | 角标能否在来源表找到 | 引用溯源断链检测（规则部分，语义匹配仍归 LLM-as-Judge） |
| `key_point_coverage` | 期望要点的字面命中率 | 快速回归哨兵，对照 LLM-as-Judge |

外加 `aggregate`（逐题指标求均值）与 `measure`（一次算齐）。

**引用角标正则复用 `_fallbacks._extract_citation_ids`**，不另写一份——避免同一规则两处维护。

### 定位声明

规则型指标会低估同义表述（要点写「状态机」而报告写「有向图」即判缺失），
**不替代语义评估**，只作「改动是否引入退化」的哨兵。已写入模块 docstring。

## 改动清单

| 文件 | 改动 |
| --- | --- |
| `app/mult_agents/eval_metrics.py` | **新增**：规则型指标层 |
| `app/test/test_eval_metrics_rules.py` | **新增**：24 用例（18 指标 + 6 token 统计） |
| `app/test/eval_metrics.py` | `TokenAccumulator` 改为回调；`run_single_query` 经 config 注入 callbacks；接入 `measure_quality`；报告与终端摘要新增 4 项指标；清理未使用导入 `os`/`Any` |

## 验证证据

| 项 | 结果 |
| --- | --- |
| 新增测试 | `test_eval_metrics_rules.py` **24 passed** |
| 后端全量（排除回归文件） | **594 passed, 2 skipped**（A 阶段结束态 570 → +24） |
| 回归专项（deselect 3 个需 MQ 的类） | **116 passed, 7 deselected**（与基线一致） |
| 前端 | **6 文件 71 passed**（与基线一致） |
| **人为漂移验证** | 让 `_usage_from` 直接 `return None` → 3 个 token 断言 **FAILED**；还原后 24 passed |
| 编译检查 | `compileall` 通过；AST 扫描确认无未使用导入 |

## 未验证 / 待实机回归

- **端到端评测未运行**：`app/test/eval_metrics.py` 与 `scripts/run_rag_eval.py` 都需要
  真实模型 API + Milvus + 可访问的搜索引擎，本环境不具备。本次只验证了可离线部分。
- **`ChatTongyi` judge 未换通道**：`build_judge_llm` 仍用 `langchain_community.ChatTongyi`。
  实测**可导入**，但项目主链路已迁到 `ChatOpenAI` 兼容通道，两者行为是否一致未验证。
  换通道需要真实 API 调用才能确认，故未动——**这是下一个待办**。
- **Golden Set 仍缺「期望引用的来源」**：现有 `key_points` 是主题词列表，
  只能测「有没有讲到」，测不了「引用对不对」。补这一列需要领域判断，建议由用户主导。
