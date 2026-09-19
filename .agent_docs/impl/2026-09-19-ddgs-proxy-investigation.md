# 排查：ddgs 为什么超时——「我配了代理」为什么没生效

- 日期：2026-09-19
- 触发：用户提问「ddgs 为什么会链接超时，我不是配置了代理吗」
- 后续追加：用户要求「只配置一个搜索源，判定 ddgs 到底可不可用，不可用则改用 ddgs 的 MCP 模式」
- **本文含一次自我更正**：初版结论「ddgs 不读 `HTTP_PROXY`」是**错的**，见 §4

## 0. 最终结论（先给答案）

| 问题 | 答案 |
| --- | --- |
| ddgs 到底可不可用？ | **不可用。** 显式传可用代理后连续 6 次全部失败（**0/6**），http 与 socks5 两种协议都不行 |
| 换 ddgs 的 MCP 模式能解决吗？ | **不能。** MCP 工具的实现就是 `DDGS(proxy=...).text(...)` —— 同一个库、同一批引擎、同一条网络路径 |
| 现在有可用的搜索源吗？ | **一个都没有。** `.env` 与环境变量里都没有 `TAVILY_API_KEY` / `SEARX_URL` |

**因此「只配置一个搜索源」这件事，在当前凭据下无法完成** ——
需要一个可用源（见 §8）。

## 1. 先确认：代理本身是好的

`.env` 配的是 `HTTP_PROXY=http://127.0.0.1:7897`。实测走它：

| 目标 | 结果 |
| --- | --- |
| www.google.com | **302**（1.1s） |
| www.bing.com/search | **200**（0.87s） |
| en.wikipedia.org | **301**（1.9s） |
| api.tavily.com | **200**（3.5s） |
| **duckduckgo.com / html.duckduckgo.com** | **超时** |

代理没问题，**唯独 DuckDuckGo 走不通**。

## 2. 调研：其他项目怎么用 ddgs

### 官方文档（PyPI）

```python
ddgs = DDGS(proxy="socks5h://127.0.0.1:9150", timeout=10, verify=True)
```

| 参数 | 说明 |
| --- | --- |
| `proxy` | `str`，支持 `http` / `https` / **`socks5`**，格式 `http://user:pass@host:port`，默认 `None` |
| `timeout` | **int，默认 5 秒**，作用对象是 HTTP 客户端（不是整条检索） |
| `verify` | SSL 校验开关或 PEM 路径，默认 `True` |
| `backend` | **单个或逗号分隔的多个后端**，默认 `"auto"` |

`text()` 可用后端：`bing`、`brave`、`duckduckgo`、`google`、`grokipedia`、`mojeek`、
`startpage`、`yandex`、`yahoo`、`wikipedia`。

### DeepWiki（由源码生成，比二手教程可靠）

| 变量 | 用途 |
| --- | --- |
| `DDGS_PROXY` | **API server 与 MCP 工具**的默认代理，经 `_expand_proxy_tb_alias` 处理 |
| `HTTP_PROXY` / `HTTPS_PROXY` | 底层 `httpx` 与 `primp` 客户端会尊重 |

### 社区实践（CSDN 教程，质量参差，仅供参考）

- 引擎不可用时的官方推荐手段就是 `backend="auto"`（自动转移）或手动 `backend="bing"`
- 重试封装：`try/except DDGSException` + `time.sleep(2)` + 最多 3 次，失败返回 `[]`
- 限流对策：每次请求间隔 1–3 秒、分时段、多代理轮换
- ⚠️ 该教程**没有**代理配置代码、没有 env var 说明、没有 socks5；且被抓取工具标出
  多处技术性错误（安装命令语言标注混乱、返回字段 `url` vs 实际 `href` 存疑、
  `DDGSException` 导出名随版本变化）。**不宜作为实现依据。**

## 3. 可用性判定（用户要求的核心问题）

### 逐个后端实测（显式 `proxy=http://127.0.0.1:7897`）

| backend | 结果 | 耗时 |
| --- | --- | --- |
| auto | 偶发成功（3 条） | 12.5s |
| bing | 失败 TimeoutException | 11.6s |
| google / mojeek / wikipedia / startpage / brave | 失败 DDGSException | 0.9–2.5s |
| yandex | 失败 DDGSException | 12.5s |
| duckduckgo | 失败 DDGSException | 5.0s |

### 但 `auto` 的那次成功是偶发 —— 连跑 6 次：**0/6**

```
第 1..6 次 -> 全部失败（DDGSException ×5、TimeoutException ×1），11.5–13.4s
成功率: 0/6
```

### 排除掉几个常见嫌疑

| 嫌疑 | 实验 | 结论 |
| --- | --- | --- |
| 代理协议不对 | `http` / `socks5h` / `socks5` 三种都试 | 全部失败，**不是协议问题** |
| `NO_PROXY` 里的 `0.0.0.0` 被当通配符 | 三种 NO_PROXY 取值对比 | 无差异，**不是它** |
| TLS 指纹伪装（`impersonate="random"`） | `primp.Client(proxy=7897, impersonate=...)` 直打 bing | **200 / 0.8s**，**不是它** |
| 网络栈本身 | `primp` 直连与走代理访问 bing 都 200 / 0.4s | 网络栈正常 |

**结论：问题在 ddgs 的引擎实现与其请求构造，不在网络、代理或底层客户端。**

### 项目 Provider 端到端验证

```
HTTP_PROXY = http://127.0.0.1:7897
_resolve_proxy() = http://127.0.0.1:7897        ← 代理解析正确、也确实传给了 DDGS
web_search_records -> 0 条  耗时 14.5s
```

即：**代理接对了也没用**，ddgs 取不到结果。

## 4. ⚠️ 自我更正：初版结论是错的

初版本文写的是「**ddgs 不读 `HTTP_PROXY`**」，依据是：

> 把代理指向没人监听的端口（`127.0.0.1:9`），ddgs 仍是慢超时 12.3s
> 而非快速连接被拒 —— 说明它压根没走代理。

**这个推断站不住脚。** 后续做了更干净的实验：

1. `primp.Client()` 在 `HTTP_PROXY=127.0.0.1:9` 下 **2.05s 快速 ConnectError** ——
   底层客户端确实读了环境变量；
2. 受控矩阵里「不设代理」与「设代理」的异常类型和耗时都不同 —— ddgs 行为随之改变。

**修正后的结论：ddgs 经 `primp` 确实尊重 `HTTP_PROXY`/`HTTPS_PROXY`。**
初版误判的原因是把「慢」直接当成「没走代理」——而 ddgs 的 `auto` 后端会依次尝试多个引擎，
单个失败快、累积起来仍是十几秒，**耗时不能用来判断是否走了代理**。

（顺带更正：`DDGS_PROXY` 在 PyPI 文档里没写，但确实存在于源码 `ddgs/api_server/`
——它是 **API server 与 MCP 工具**的默认代理，不是库的通用默认值。）

### 但一个 A/B 对照仍未解释

| 调用方式 | 结果 |
| --- | --- |
| `DDGS(proxy="http://127.0.0.1:7897")` | 偶发成功（6 次中 1 次） |
| `DDGS()` + 环境变量 `HTTP_PROXY=7897` | 全部失败 |

源码 `ddgs/http_client.py` 是 `primp.Client(proxy=proxy, ...)`（显式传参）。
两者行为差异**未完全解释**，但**不影响结论**：显式传参是最好的情况，也只是 0/6。

## 5. 为什么 MCP 模式救不了

ddgs 的 MCP 工具实现（`ddgs/api_server/mcp.py:45`）：

```python
lambda: DDGS(proxy=_expand_proxy_tb_alias(os.environ.get("DDGS_PROXY"))).text(...)
```

**同一个 `DDGS` 类、同一批引擎、同一条网络路径。** 我在 §3 测的就是
`DDGS(proxy=...).text(...)` 这条路径（0/6）。

换 MCP 只是把调用入口从「进程内函数调用」换成「跨进程 MCP 协议」，
**引擎可达性不会因此改变**。除非引入 MCP 的同时**也换掉搜索引擎**，
否则这一改动不解决问题。

## 6. 本次代码改动及其真实价值

`DuckDuckGoProvider` 新增 `_resolve_proxy()`，在 `_ddgs()` 中显式传参：

```python
def _ddgs(self):
    return DDGS(proxy=self._resolve_proxy())
```

优先级 `DDGS_PROXY` > `HTTPS_PROXY` > `HTTP_PROXY`（大小写变体都覆盖），
空串视为未配置（空串会被 ddgs 当 URL 解析而报错）。

**真实价值**：① 支持 `DDGS_PROXY`（`primp` 不认它）；② 代理来源在代码里可见。
**不承诺**：它不改变「ddgs 取不到结果」这一事实。

## 7. 顺带踩到的 Windows 坑

写测试时 `monkeypatch.setenv("HTTP_PROXY", ...)` 之后又
`monkeypatch.delenv("http_proxy", raising=False)`，把刚设的值删掉了：
**Windows 的环境变量名大小写不敏感**，两者是同一个变量。已改为先清完再设值。

## 8. 建议：单一搜索源该怎么配

**前提：需要一个可用源。** 实测可达的有 `api.tavily.com`（200）。

| 方案 | 需要什么 | 备注 |
| --- | --- | --- |
| **Tavily**（推荐） | `TAVILY_API_KEY` | 官方文档说「已针对 LLM 优化」；`api.tavily.com` 实测可达 |
| **SearXNG** | 自建实例 + `SEARX_URL` | 无外部依赖，但需自己部署 |
| ~~ddgs~~ | — | **本环境不可用（0/6）**，无论库调用还是 MCP |

配置方式：`config.json` 的 `search_providers` 只留一个名字，
凭据放 `.env`（`TAVILY_API_KEY` 或 `SEARX_URL`）。

### 已存在但容易漏掉的两条保护

`tools.py` 的链初始化已经做了启动期校验，会打印：

- `[search-chain] 可用搜索源: ...`
- `[search-chain] 以下搜索源因未配置而跳过: ...`
- 以及无可用源时的 `logger.error("没有任何可用的搜索源…")`

**但有一个缺口**：`DuckDuckGoProvider.available` 是**静态返回 `True`**，
不做可达性探测 —— 所以链会报「可用搜索源: DuckDuckGoProvider」，
而它实际上一次都取不到。这就是「看起来配好了、实际全空」的来源。

## 9. 验证

| 项 | 结果 |
| --- | --- |
| 后端全量 | **654 passed, 2 skipped** |
| 回归专项 | **116 passed, 7 deselected** |
| **人为漂移验证** | `_resolve_proxy` 不读任何变量 → `TestDDGSProxyWiring` **3 个断言失败**；还原后 34 passed |


---

# 追加：ddgs MCP 模式的实测结论（2026-09-19 12:xx）

用户反馈「在 SpringAI 项目里用过 ddgs 的 MCP，可以返回数据」，要求改用 MCP 模式。
实测后的结论：**在本项目里走不通，且原因与 SpringAI 场景不同。**

## 1. `ddgs mcp` 起不来：依赖版本不够

```
Error: MCP dependencies not installed. Run: pip install 'ddgs[mcp]'
```

根因：`ddgs` 声明 `mcp>=2.0; extra == "mcp"`，而环境里是 **mcp 1.12.1**
（只有旧 API `mcp.server.fastmcp`，没有 `ddgs/api_server/mcp.py` 要的
`mcp.server.mcpserver`）。

## 2. ⚠️ 装 `mcp>=2.0` 会把项目搞坏

`pip install "mcp>=2.0"` 的解析结果是 `mcp 2.2.0` + **`starlette 1.6.0`**，
而项目的 `fastapi 0.123.0` 要求 `starlette<0.51.0`。实测后果：

```
TypeError: Router.__init__() got an unexpected keyword argument 'on_startup'
```

**FastAPI 连 app 都建不起来** —— 后端直接不可用。
已恢复到 `mcp 1.12.1` + `starlette 0.50.0`，并跑全量测试确认无残留
（**654 passed**）。

注：`mcp 2.2` 自己只要求 `starlette>=0.27`（Python<3.14），**是 pip 解析到了最新版**。
但把 `starlette` 钉在 `<0.51` 与 `mcp>=2.0` 一起装时，解析器最终把 `mcp` 卸掉了 ——
两者在本环境无法共存。

## 3. 为什么 SpringAI 里能跑，这里不行

**在 SpringAI 里，`ddgs mcp` 是一个独立的 Python 子进程** ——
它的 Python 依赖（mcp 2.x、starlette）装在那个子进程自己的环境里，
**与 Java 项目的依赖树互不影响**。

本项目是 **Python 项目**，`mcp` 与本项目的 `fastapi`/`starlette` 共用同一个环境，
于是 `ddgs[mcp]` 的依赖升级会直接冲击 Web 框架。**这是场景差异，不是 ddgs 的问题。**

## 4. 而且底层调用是同一条路径

`ddgs/api_server/mcp.py:45` 的工具实现：

```python
lambda: DDGS(proxy=_expand_proxy_tb_alias(os.environ.get("DDGS_PROXY"))).text(...)
```

与我在 §3 测的 `DDGS(proxy=...).text(...)` **完全相同**。
`ddgs text` CLI 子命令走同一条路径，实测也失败：

```
① 带 -pr http://127.0.0.1:7897
   DDGSException: ... error sending request for url (https://search.yahoo.com/search?...)
   > cannot decrypt peer's message
② 不带代理
   DDGSException: ... (https://www.startpage.com/) > tls handshake eof
```

## 5. 新的线索：失败发生在 **TLS 层**

`cannot decrypt peer's message` 与 `tls handshake eof` 都是 TLS 层错误，
而不是「连不上」。含义是**代理在干扰/无法透传 TLS 流**（常见于
MITM 拦截、或分流规则把部分域名直连而 TLS 会话对不上）。

这解释了此前的矛盾现象：同一代理下 `curl` 到 `bing.com/search` 是 200，
而 ddgs 的各引擎报 TLS 错误 —— 不同客户端/不同域名走的路径不一样。

**这是给用户排查代理配置的直接线索**（例如换 socks5、或检查代理的分流规则）。

## 6. 本次按用户要求做的配置变更

`config.json`：`search_providers` 由 `["ddgs", "tavily", "searxng"]` 收成 **`["ddgs"]`**
（用户明确要求「只要一个 ddgs 数据源，其他的不要」）。

**未做 MCP 接入**，理由：它会破坏 FastAPI 依赖，且底层搜索路径相同、
不会改变可达性。若用户仍要 MCP，需先解决 `mcp>=2.0` 与 `fastapi 0.123` 的共存问题
（例如升级 FastAPI，或把 MCP server 放到独立环境里以子进程方式调用）。
