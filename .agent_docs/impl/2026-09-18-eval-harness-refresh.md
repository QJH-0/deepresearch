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

## 第二轮：端到端验证暴露的三处缺陷（中间件启动后实测）

用户启动中间件后做真实运行，脚本**根本跑不起来**，逐个修掉：

### 缺陷 3：入口路径设置错误，文档里的运行命令无效

```
$ python -m app.test.eval_metrics --help
ModuleNotFoundError: No module named 'mult_agents'
```

脚本只把 `_PROJECT_ROOT` 插入 `sys.path`，但 `mult_agents` 是 `app/` 下的顶层包，
必须同时插入 `app/`。且 `app/` 与 `app/test/` 都没有 `__init__.py`，
文档里的 `python -m app.test.eval_metrics` 形式也不成立。

**修法**：同时插入两个路径；文档改为 `python app/test/eval_metrics.py`；
废弃不存在的 `--config` 参数说明。

### 缺陷 4：用同步 `invoke` 跑异步节点

```
TypeError: No synchronous function provided to "intent".
Either initialize with a synchronous function or invoke via the async API
```

所有节点都是 `async def`，脚本却用 `app.invoke(...)`。

**修法**：`run_single_query` 与 `run_eval` 改 async，用 `await app.ainvoke(...)`；
`__main__` 用 `asyncio.run(...)`。

### 缺陷 5：用同步 checkpointer 配异步执行

`build_checkpointer` 返回 sync `PostgresSaver`，而 `runtime.py` 已注明：
sync 版没有 `aget_tuple`/`aput`，`ainvoke` 会落到基类 stub 抛 `NotImplementedError`。

**修法**：改用 `await init_checkpointer(config)` / `await close_checkpointer()`。

### 缺陷 6：HITL 未关闭，评测拿到空报告

`config.json` 的 `hitl_enabled=true`，图会在 `plan` 处 `interrupt` 并返回，
`final` 为空——评测全部作废且不报错。

**修法**：三组评测配置统一 `with_overrides(..., hitl_enabled=False)`。
（`clarify.py:315-318` 已有「无人可答则直通 plan」的处理，与此一致。）

## 端到端验证：token 统计确认生效

直答路径实跑（`什么是REST API`）：

| 项 | 结果 |
| --- | --- |
| intent 路由 | `direct` ✓ |
| 耗时 | 6.7s |
| final | 828 字，内容正常 |
| **token（真实 TokenAccumulator）** | **293（prompt 168 + completion 125），解析成功 2 次调用，未解析 0** |

**关键发现：usage 不在 `llm_output` 里。** 实测 `llm_output` 恒为 `null`，
usage 实际落在 `response.generations[0][0].message.usage_metadata`
（`{"input_tokens":..., "output_tokens":..., "total_tokens":...}`）。
若只读 `llm_output`（旧实现的思路）会再次得到 0——回调的双落点回退是必要的。

## 第三轮：首次真实评测结果 + 代理误配置定位

### 评测实跑（1 道多智能体题，24 分 12 秒）

| 指标 | baseline（max_iter=1） | improved（max_iter=2） |
| --- | --- | --- |
| 研究完备性 | 1.0000 | 0.9167（5 covered + 1 partial / 6） |
| 端到端耗时 | 491.7s | 678.6s |
| Token | 42732 | 65568 |
| 幻觉率 | 40% | 10% |
| 引用准确率 | — | 100%（5/5） |
| **证据重复率** | **0.0%** | **0.0%**（28 项 / 28 唯一） |
| 检索深度 | 9.5 查询/轮 | 8.67 查询/轮 |
| 引用合法率 | — | 100% |
| 要点字面覆盖 | — | 100%（6/6） |

**本轮验证到的两件事**：

1. **A1 在真实链路上成立**：`web_evidence` / `local_evidence` / `evidence_pool`
   三个字段的重复率均为 0，28 项全唯一。重复累加确已消除。
2. **token 统计真实生效**：42732 / 65568，回调捕获成功。

**不能据此下结论的地方**（必须写清）：

- **n=1，无统计意义**。完备性 baseline > improved（1.0 vs 0.9167）是单样本噪声，
  不能读成「多迭代变差」。
- **网页检索全程返回 0 条**：18 个 web 查询 raw_count 全为 0，
  这轮只跑通了本地 RAG 路径（14 条证据全部来自同一份《AI_Agent_白皮书.md》）。
  「引用准确率 100%」实际只覆盖 5 个引用、单一来源。
- **baseline/improved 是 `max_iterations=1 vs 2`**，度量的是「多跑一轮的代价」，
  不是任何架构优化的收益。

### 报告标签修正

`print_summary` 里三处过时文案已修：`qwen-max` → 取 `meta.judge_model`；
「检索耗时」→「端到端耗时」（原标签名不符实，用的其实是整题 `elapsed_time`）；
「DashScope API usage hook」→「LangChain 回调」。并新增 `_delta_text`，
负的「降低比例」显式渲染成「增加 X%」，避免 -38% 被读成降低。

### 代理误配置定位（本轮最有价值的发现）

现象：评测中 18 个 web 查询全部返回 0 条，日志只有一条 warning。

排查链：

1. `tavily` / `searxng` **未配置**（缺 `TAVILY_API_KEY` / `SEARX_URL`）→ 被跳过，只剩 ddgs
2. 进程环境里 `HTTP_PROXY=http://127.0.0.1:2254`，实测该代理返回 **502 Bad Gateway**
3. 用户提供的 VPN 端口 `7897` 实测 **HTTP 202 / 1.0s / 14KB** ✓
4. `.env` 里**已有 `# 网络代理` 段，但只填了 `NO_PROXY`，代理地址一直是空的**
   → 于是外部注入的坏代理 2254 生效

**修法**：在 `.env` 的既有 `# 网络代理` 段补上 `HTTP_PROXY` / `HTTPS_PROXY` = `127.0.0.1:7897`。

验证（清掉外部注入的变量后）：

```
load_dotenv 前 HTTP_PROXY = （未设置）
load_dotenv 后 HTTP_PROXY = http://127.0.0.1:7897
网页检索: 耗时 2.4s，返回 4 条   ← 真实结果
```

> `.env` 已 gitignore，不随仓库分发；换机器需各自配置。
> `load_dotenv` 默认**不覆盖**已存在的环境变量，故若 shell 里已注入 `HTTP_PROXY`，
> 需先 `unset` 才能让 `.env` 生效。

### 一次被推翻的假设（记录以免重犯）

排查过程中我一度判定「`DDGS()` 未透传 timeout 导致 `search_timeout_seconds` 不可达」，
并改了 `tools.py` 与 `config.json`。

**对照实验推翻了它**：三个变体（有/无 `with`、中/英文）全部失败，错误是
`proxy CONNECT`，与 timeout 无关。且两者**作用域不同**——
`search_timeout_seconds` 是「整条 Provider 链」上限，ddgs 的 `timeout` 是「单次 HTTP 请求」
上限；透传只会把每次请求的等待拉长（15s→30s），与「源不可达时快速失败」的初衷相反。

**已全部回退**（`tools.py` 恢复 `DDGS()`、`config.json` 恢复 15s），
并把这次结论固化成 `test_search_provider.py::TestSearchTimeout`
（含一条断言「DDGS 不得被塞入链路级 timeout」），防止以后又有人这么改。

**教训**：报错信息可能与真实原因无关。先做对照实验分离变量，再改代码。

---

## 第四轮：代理修复后重跑（同一道题，可直接对比）

### 代理修复是决定性的

| 检索统计（improved） | 修复前 | 修复后 |
| --- | --- | --- |
| web 查询 / raw / kept | 18 / **0** / 0 | 18 / **38** / **38** |
| local 查询 / raw / kept | 8 / 16 / 14 | 18 / 26 / 26 |
| 总证据 | 14 | **64**（4.6×） |
| 正文引用数 | 5 | **18**（3.6×） |

### 质量与成本

| 指标 | 修复前 | 修复后 | 读法 |
| --- | --- | --- | --- |
| 研究完备性 | 0.9167 | **1.0**（6 covered / 0 partial / 0 missing） | ↑ |
| 幻觉率 | 0.1 | **0.0**（0 / 30 句） | ↑ |
| 引用准确率 | 1.0（仅 5 个引用） | 0.9444（17 / 18） | 样本变大后暴露 1 处不匹配 |
| 引用合法率 | 1.0 | **1.0**（18 / 18） | = |
| 要点字面覆盖 | 1.0 | **1.0**（6 / 6） | = |
| **证据重复率** | 0.0%（28 项） | **0.0%（128 项）** | **A1 在 4.6× 证据量下仍成立** |
| 检索深度 | 8.67 查询/轮 | **12.0**（3 轮 × 12） | 双路检索恢复后的真实深度 |
| 低质信源率 | 0.0 | 0.4531 | 有了 web 来源才测得出，网页质量参差 |
| Token | 65568 | 178288（2.7×） | 成本随证据量上升 |
| 端到端耗时 | 678.6s | 876.5s（1.29×） | — |

**最有价值的一条**：证据重复率在 **128 项**（38 web + 26 local + 64 pool）下仍为 **0.0%**。
相比修复前的 28 项，这是对 A1 强得多的验证——若 reducer 语义还有问题，
证据量放大 4.6 倍后重复会立刻暴露。

### 意外发现：A3 的证据预算实际在裁剪

`evidence_pool` 实测 **64 条 > 默认 `context_evidence_limit=40`**，
即 `analyze` 只看到排序后的前 40 条——**A3 的预算并非「几乎不触发的保险丝」**。

这与我在 A3 设计时的判断不符（原 docstring 写的是「只在异常召回数百条时才生效」）。
已修正三处文档使其与实际行为一致（`_context.py` docstring、
`Settings`/`AppConfig` 的字段注释），并写明两种取向：

- 想只做保险丝 → 把 limit 提到明显高于常态召回量
- 想主动控成本 → 保持 40，但**必须用 Golden Set 验证是否伤及完备性**
  （该轮完备性 1.0、幻觉率 0，未见损伤，但 n=1）

引用完整性不受影响：`deep_dive` 的确定性回填会把被裁证据补进证据池，只是拿先验分。

### 仍不能下的结论

- **n=1**。完备性/幻觉率的「提升」是单样本，不能当作 A 阶段的收益证明。
- **baseline/improved 仍是 `max_iterations=1 vs 2`**，度量的是「多跑一轮的代价」，
  不是任何架构优化的收益。要测架构收益得先有 Golden Set 的「期望引用来源」列。

---

## 第五轮：Golden Set 判别力诊断与数据化

### 诊断：当前题集测不出任何改进

用已有两次评测的数据核对（同一道题，baseline vs improved）：

| 阶段 | 完备性 | 要点字面覆盖 |
| --- | --- | --- |
| baseline（仅 1 轮迭代） | **1.0** | **1.0** |
| improved（2 轮迭代） | 1.0 | 1.0 |

**连只跑 1 轮的 baseline 都拿满分 → 指标触顶，零判别力。**

根因（有数据支撑）：`key_points` 是**框架名 + 通用概念**
（`LangGraph` / `AutoGen` / `CrewAI` / `状态机` / `工具调用`）。
全部 30 道多智能体题共 163 个 key_point，**只有 2 个含数字或年份** ——
其余都是「提到就得分」的软词，任何一篇像样的研报都会写到。

所以问题不是「缺期望来源」，而是**要点本身没有判别力**。
在修好这一点之前，跑再多题、做再多架构改动，指标都会是满分。

### 改动

| 文件 | 改动 |
| --- | --- |
| `app/test/golden_set.json` | **新增**：题集从 Python 字面量抽成 JSON 数据文件（50 题），带 `_readme` 说明字段语义与判别力要求 |
| `app/test/eval_metrics.py` | 新增 `load_golden_set()`；删掉 82 行内联题集；`measure_quality` 透传 `expected_sources`；新增 `--judge-rounds`（跑大样本时降裁判轮数省 2/3 成本）；摘要新增第 11 项指标 |
| `app/mult_agents/eval_metrics.py` | 新增 `expected_source_recall`；`measure` 增加 `expected_sources` 参数 |
| `app/test/test_eval_metrics_rules.py` | 新增 `TestExpectedSourceRecall`（5 用例） |

### 设计要点

**`expected_source_recall` 未标注时返回 `None` 而非 `0.0`。**
`aggregate` 只对数值取均值，`None` 会被自动跳过 ——
否则「这题没标注」会被误算成「一个来源都没召回」，把均值拖下来。
该语义已用测试锁死（漂移验证：改成 `0.0` → 断言失败）。

**题集是数据不是代码。** 标注期望要点与期望来源需要反复编辑与评审，
放 JSON 比改 Python 字面量安全，也便于 diff 审阅。

**判别力要求写进 JSON 的 `_readme`**，与数据同处一地（不另写文档，避免两处维护）：

```
✅ 「LangGraph 用 checkpointer 支持断点续研」   ← 需要读到具体机制
✅ 「AutoGen v0.4 重构为异步 actor 模型」       ← 需要读到具体版本变更
❌ 「LangGraph」                                ← 提到名字就得分
❌ 「多智能体」                                  ← 任何一篇都写
建议每题 4-6 条，其中至少一半是「具体机制 / 版本 / 数字 / 对比结论」
```

### 待用户完成的部分

**30 道题的 `key_points` 需要按判别力要求重写，`expected_sources` 需要标注。**
这两项都需要领域判断——尤其要点若写错（写了不存在的事实），
整个评测会被污染，所以**不适合由 AI 单方面代笔**。
框架、指标、成本旋钮都已就位，填完即可用。

### 验证

| 项 | 结果 |
| --- | --- |
| 新增测试 | `TestExpectedSourceRecall` 5 用例，`test_eval_metrics_rules.py` 共 **29 passed** |
| 后端全量 | **602 passed, 2 skipped** |
| 回归专项 | **116 passed, 7 deselected**（与基线一致） |
| 前端 | **6 文件 71 passed**（与基线一致） |
| 题集加载 | `load_golden_set()` 读出 50 题（30 multiagent + 20 direct） |
| **人为漂移验证** | `recall` 由 `None` 改成 `0.0` → `test_not_applicable_when_no_expectations` **FAILED**；还原后 29 passed |

---

## 第六轮：找到不依赖人工标注的判别指标 + 抽出引用角标模块

### 关键发现：判别指标已经存在，只是没被采集

`write` 节点一直有 P7-2 的引用覆盖率埋点，但只写进日志、没进评测报告。
把两次运行的日志拉出来对比：

| 运行 | baseline（1 轮） | improved（2 轮） |
| --- | --- | --- |
| 代理故障（web 全空） | 28.4%（27/95） | 21.8%（24/110） |
| 代理修复后 | **62.1%**（36/58） | 35.6%（31/87） |

**在 21.8% ~ 62.1% 之间波动 —— 有判别力，且不依赖任何人工标注。**
代理修复后 baseline 直接从 28.4% 跳到 62.1%，对检索质量高度敏感。

**含义**：B 阶段不必等 Golden Set 改好才能度量，`citation_coverage` 就能给出信号。
（仍建议同时补 Golden Set 的要点判别力，两者互补：一个测「论断有没有来源支撑」，
一个测「有没有覆盖到该覆盖的事实」。）

顺带发现：**迭代越多，引用覆盖率反而下降**（62.1% → 35.6%；21.8% vs 28.4%）。
原因是报告变长（58→87 句、95→110 句），分母涨得比带角标的句子快。
这本身是个值得跟进的信号：多跑一轮是否在稀释论断的引用密度。

### 抽出 `mult_agents/citations.py`

把 `citation_coverage` 接进 `write` 时踩到**循环导入**：

```
eval_metrics → nodes._fallbacks → nodes/__init__ → write → eval_metrics（未初始化完）
ImportError: cannot import name 'citation_coverage' from partially initialized module
```

根因是引用角标逻辑放在了 `nodes/` 下，而评测模块要用它。
且该正则当时**散落三处**（`nodes/_fallbacks.py`、`backend/service/report_check.py`、
`nodes/write.py` 各一份）。

**修法**：新建顶层 `app/mult_agents/citations.py` 作为唯一实现
（`CITATION_ID_PATTERN` / `extract_citation_ids` / `validate_and_fix_citations`）：

- `_fallbacks.py` 改为导入并保留私有别名 → 既有调用方与测试的 patch 目标不变
- `eval_metrics.py` 从 `citations` 导入 → **不再依赖 `nodes`，环解开**
- `report_check.py` 删掉自己的正则与 `_extract_citations`，改用共享实现
  （原注释「与 nodes/_fallbacks.py 的引用格式保持一致」正是重复的信号）

### 改动清单

| 文件 | 改动 |
| --- | --- |
| `app/mult_agents/citations.py` | **新增**：引用角标唯一实现 |
| `app/mult_agents/eval_metrics.py` | 新增 `citation_coverage`；改从 `citations` 导入；`measure` 纳入新指标 |
| `app/mult_agents/nodes/write.py` | P7-2 埋点改调用共享实现（删掉内联的正则与句长阈值） |
| `app/mult_agents/nodes/_fallbacks.py` | 两个引用函数改为导入别名；清理因此不再使用的 `re` |
| `app/backend/service/report_check.py` | 删掉重复的正则与 `_extract_citations`，改用共享实现 |
| `app/test/test_eval_metrics_rules.py` | 新增 `TestCitationCoverage`（4 用例）；`TestMeasure` 补新指标 |
| `app/test/eval_metrics.py` | 摘要新增「10b. 引用覆盖率」 |

### 验证

| 项 | 结果 |
| --- | --- |
| 后端全量 | **606 passed, 2 skipped** |
| 回归专项 | **116 passed, 7 deselected**（与基线一致） |
| 前端 | **6 文件 71 passed**（与基线一致） |
| 循环导入 | 先导入 `eval_metrics` 再导入 `write` 均成功（此前必炸） |
| 单一维护源 | 角标正则由 3 处收敛为 1 处；`test_matches_write_node_embedding` 锁住节点与评测同源 |
| **人为漂移验证** | `SUBSTANTIVE_SENTENCE_MIN_CHARS` 由 15 改为 0 → `test_counts_only_substantive_sentences` **FAILED**；还原后 33 passed |

### 一次自我纠错

新写的两个测试一开始就失败，**是我的期望写错了**（把引用角标放在了被排除的短句里，
以及 `measure` 的用例正文不足 15 字导致分母为 0）。已修正测试而非改实现——
实现行为是对的。

---

## 第一轮：离线审查发现的两处缺陷

（编号按发现顺序；第二轮、第三轮的内容在本文档更靠前，因为它们是后续补记的。）

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
| `app/test/eval_metrics.py` | `TokenAccumulator` 改为 LangChain 回调；判官模型换 ChatOpenAI 兼容通道（`build_judge_llm` 委托 `build_aux_llm`）；`run_eval`/`run_single_query` 改 async + `ainvoke` + `init_checkpointer`；修 `sys.path` 入口；三组配置关 HITL；接入 `measure_quality`；报告与摘要新增 4 项指标；修 3 处过时文案；清理未使用导入 |
| `app/test/test_search_provider.py` | 新增 `TestSearchTimeout`（3 用例），锁定超时取值与「DDGS 不得被塞入链路级 timeout」 |
| `.env` | 补 `HTTP_PROXY`/`HTTPS_PROXY` = `127.0.0.1:7897`（**已 gitignore，不入库**） |

## 验证证据

| 项 | 结果 |
| --- | --- |
| 新增测试 | `test_eval_metrics_rules.py` 24 + `test_search_provider.py` 3 |
| 后端全量（排除回归文件） | **597 passed, 2 skipped** |
| 回归专项（deselect 3 个需 MQ 的类） | **116 passed, 7 deselected**（与基线一致） |
| 前端 | **6 文件 71 passed**（与基线一致） |
| **人为漂移验证** | `_usage_from` 返回 `None` → 3 个 token 断言 **FAILED**；还原后 24 passed |
| **端到端评测实跑** | 1 道多智能体题，24 分 12 秒完成，产出完整报告（见「第三轮」） |
| **token 统计端到端** | 直答 293 tokens / 2 次调用 / 0 未解析；评测 42732 → 65568 |
| **A1 端到端验证** | 证据重复率 **0.0%**（28 项全唯一） |
| **代理修复验证** | 清掉外部注入变量后 `.env` 生效，网页检索 **2.4s 返回 4 条真实结果** |
| 编译检查 | `compileall` 通过；AST 扫描确认无未使用导入 |

## 未验证 / 待办

1. **网页检索路径尚未在评测中跑过**：上一轮评测（18 个 web 查询全 0 条）是在代理修好之前跑的。
   修好代理后需要**重跑一次评测**，才能得到真正覆盖「web + local 双路检索」的基线。
2. **Golden Set 仍缺「期望引用的来源」**：现有 `key_points` 是主题词列表，
   只能测「有没有讲到」，测不了「引用对不对」。补这一列需要领域判断，建议由用户主导。
3. **`tavily` / `searxng` 未配置**：当前只有 ddgs 一个可用检索源。
   配一个 `TAVILY_API_KEY` 可显著提升检索稳定性（不依赖代理）。
4. **样本量**：Golden Set 有 50 题，目前只跑了 1 题。全量跑一轮预计数小时。
5. **`rag/core.py` 重复定义了兼容通道 base URL**（`os.getenv("BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")`
   与 `models._DASHSCOPE_COMPAT_BASE_URL` 重复）。**未改**：`models.py` 已 import `rag.core`，
   反向 import 会成环，需要先抽到中立模块。按「不顺手扩大范围」只记录。
