# B3/B4 端到端结果：本轮对照无效（搜索引擎大面积超时）

- 日期：2026-09-19
- 运行：`output/eval_b34_smoke.json` / `output/eval_b34.log`
- 结论：**本轮阶段对照不可用**，B3/B4 的收益仍未有有效测量

## 为什么本轮无效

baseline 阶段的 **20 次 web 检索全部失败**，拿到 0 条 web 证据；
improved 阶段拿到 18 条。两阶段的检索条件根本不同，指标差异无法归因于代码改动。

日志中的失败类型（共 91 条错误）：

| 次数 | 错误 |
| --- | --- |
| 38 | `duckduckgo: TimeoutException`（`html.duckduckgo.com` 超时） |
| 3 | `startpage: ConnectError`（目标计算机积极拒绝，os error 10061） |
| 各 1 | `yahoo` / `google` 超时、`DDGSException(cannot decrypt peer's message)` |

评测以 `env -u HTTP_PROXY` 直连运行，本网络下这几个引擎基本不可用。

## 原始数据（仅作记录，不作结论）

| 指标 | baseline（1 轮） | improved（2 轮） |
| --- | --- | --- |
| 运行错误 | 无 | 无 |
| 证据重复率 | 0.0 | 0.0 |
| web 保留 / 检索次数 | **0 / 20** | 18 / 28 |
| local 保留 | 41 | 47 |
| 外层检索轮次 | 2 | 3 |
| 平均每轮查询数 | 20.0 | 17.33 |
| token | 132724 | 206812（+56%） |
| 端到端耗时 | 523s | 744s（+42%） |
| 引用合法率 | 1.0 | 1.0 |
| 引用覆盖率 | 0.4906 | 0.6053 |
| 引用准确率 | — | 0.5 |
| 低质信源占比 | 0.0 | 0.2154 |
| 幻觉率 | 0.4333 | 0.0667 |
| 完备性 | 0.8333 | 0.8333 |
| 要点字面覆盖 | 1.0 | 1.0 |

**注意 `引用覆盖率 0.4906 → 0.6053` 不能读作改进**：baseline 没有 web 证据，
无角标可打，覆盖率自然低。这正是「覆盖率对『有没有证据』敏感、对『检索多深』不敏感」
的又一例证（见 `2026-09-19-citation-coverage-ceiling.md`）。

## 顺带确认的事

1. **B1 的两个修复在 B3/B4 之后依然成立**：无 `Recursion limit` 报错，
   两阶段证据重复率均为 0.0。
2. **完备性 0.8333**（此前为 1.0）。baseline 5/6 覆盖、improved 4/6 覆盖 + 2 部分覆盖，
   数值恰好相同。n=1 且本轮对照无效，**不能判定为 B3/B4 导致的退化**。
3. **要点字面覆盖仍是 1.0** —— Golden Set 的 `key_points` 判别力问题依旧，
   与 B3/B4 无关。

## 为此新增的防护：`retrieval_health`

本轮暴露的流程缺陷是「污染无法从报告里看出来」—— 只看 JSON 会以为
「baseline 引用覆盖率低 = 改动有害」。已给评测加 `retrieval_health`：

```json
"retrieval_health": {
  "baseline": {"web_queries": 20, "web_kept": 0, "yield_rate": 0.0, "degraded": true},
  "improved": {"web_queries": 28, "web_kept": 18, "yield_rate": 0.6429, "degraded": false},
  "degraded_phases": ["baseline"]
}
```

`degraded_phases` 非空时，`print_summary` 会直接打印
「本轮对照无效：baseline 阶段一条 web 证据都没拿到」。
「没检索过」与「检索全失败」被区分开（前者 `yield_rate` 为 `None`，不报 degraded）。

## 下一步

要让 B 阶段的收益可测，需要先解决**评测环境本身的稳定性**：

1. **配置可用的搜索源**。`config.json` 的 `search_providers` 支持 tavily / searxng，
   本网络下 ddgs 不可靠。没有稳定的检索源，任何检索相关指标都在噪声里。
2. 或退一步：把评测限定为 **local-only**（关掉 web），先测「检索策略」与
   「分析/成文」部分的可复现收益，把 web 的不确定性隔离在外。
3. Golden Set 的 `key_points` 判别力（用户侧任务）—— 这是「检索深度收益」
   唯一缺的判别指标。
