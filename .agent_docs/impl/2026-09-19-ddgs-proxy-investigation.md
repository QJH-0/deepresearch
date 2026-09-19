# 排查：ddgs 为什么超时——「我配了代理」为什么没生效

- 日期：2026-09-19
- 触发：用户提问「ddgs 为什么会链接超时，我不是配置了代理吗」
- **本文含一次自我更正**：初版结论「ddgs 不读 `HTTP_PROXY`」是**错的**，见 §4

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
| `HTTP_PROXY` / `HTTPS_PROXY` | **底层 `httpx` 与 `primp` 客户端会尊重**的标准环境变量 |

### 社区实践（CSDN 教程，质量参差，仅供参考）

- 引擎不可用时的官方推荐手段就是 `backend="auto"`（自动转移）或手动 `backend="bing"`
- 重试封装：`try/except DDGSException` + `time.sleep(2)` + 最多 3 次，失败返回 `[]`
- 限流对策：每次请求间隔 1–3 秒、分时段、多代理轮换
- ⚠️ 该教程**没有**代理配置代码、没有 env var 说明、没有 socks5；且被抓取工具标出
  多处技术性错误（安装命令语言标注混乱、返回字段 `url` vs 实际 `href` 存疑、
  `DDGSException` 导出名随版本变化）。**不宜作为实现依据。**

### 对本项目的三点直接启示

1. **`backend="auto"` 的「自动转移」是按引擎顺序试，每个引擎各花自己的 timeout** ——
   本网络下 DDG 不可达、brave 429，于是每次检索都要把不可用的引擎挨个试一遍，
   累积成十几秒的等待。**显式指定 `backend="bing,..."` 可跳过不可用引擎。**
2. `timeout` 默认只有 **5 秒**（本项目没传，用的就是默认值）。
3. `proxy` 支持 **socks5** —— 如果 7897 是 HTTP 代理而 DDG 对 HTTP 代理不友好，
   换 socks5 值得一试。

## 3. 三层原因（更正后）

### 第一层（线上直接原因）：shell 的 `HTTP_PROXY` 盖掉了 `.env`

```
shell : HTTP_PROXY=http://127.0.0.1:4401   ← WorkBuddy 注入
.env  : HTTP_PROXY=http://127.0.0.1:7897
```

`load_dotenv` **默认不覆盖已存在的环境变量**（`.env` 里就写着这条警告），于是 4401 赢。
而实测走 4401 访问 google **超时**（curl 退出码 124）——4401 是给 agent 自身工具用的，
不是通用出口代理。

**ddgs 拿到的是 4401，所以全部超时。这就是「配了代理却超时」的直接答案。**

我此前跑评测时加 `env -u HTTP_PROXY -u HTTPS_PROXY ...` 正是为了绕开它
（让 `.env` 的 7897 生效）。

### 第二层：即使换成可用的 7897，ddgs 仍取不到结果

受控矩阵实验（`backend=bing`）：

| 配置 | 结果 | 耗时 |
| --- | --- | --- |
| 不设代理 | TimeoutException | 20.13s |
| 死代理 `127.0.0.1:9` | DDGSException | 10.24s |
| 好代理 `7897` | DDGSException | 12.58s |

②③ 与 ① 的异常类型完全不同 → **代理确实被读到了**。但换用可用的 7897 后仍失败，
尽管 `curl -x 7897 https://www.bing.com/search` 返回 **200**。
brave 走 7897 是 **429 限流**，duckduckgo 走 7897 **超时**。

**这一层的原因尚未查明** —— ddgs 的请求构造（headers/cookies/参数）与 curl 不同，
可能被代理的上游规则或引擎侧拦截。底层 `primp` 本身正常
（bing 直连与走代理都是 200 / 0.4s）。

### 第三层：降级链是空的

`search_providers = ["ddgs", "tavily", "searxng"]`，但 `tavily_api_key` 与
`searxng_base_url` **都没配** → 链上只有 ddgs 一个真在跑 → **全链失败**。

## 4. ⚠️ 自我更正：初版结论是错的

初版本文写的是「**ddgs 不读 `HTTP_PROXY`**」，依据是：

> 把代理指向没人监听的端口（`127.0.0.1:9`），ddgs 仍是慢超时 12.3s
> 而非快速连接被拒 —— 说明它压根没走代理。

**这个推断站不住脚。** 后续做了两个更干净的实验：

1. `primp.Client()` 在 `HTTP_PROXY=127.0.0.1:9` 下 **2.05s 快速 ConnectError** ——
   底层客户端确实读了环境变量；
2. 受控矩阵里「不设代理」与「设代理」的异常类型和耗时都不同 —— ddgs 的行为随之改变。

**修正后的结论：ddgs 经 `primp` 确实尊重 `HTTP_PROXY`/`HTTPS_PROXY`。**
初版之所以误判，是因为把「慢」直接当成「没走代理」——而 ddgs 的 `auto` 后端会依次
尝试多个引擎，单个失败快、累积起来仍是十几秒，**耗时不能用来判断是否走了代理**。

（顺带更正：`DDGS_PROXY` 在 PyPI 文档里没写，但确实存在于源码 `ddgs/api_server/`
——它是 **API server 与 MCP 工具**的默认代理，不是库的通用默认值。）

## 5. 本次代码改动及其真实价值

`DuckDuckGoProvider` 新增 `_resolve_proxy()`，在 `_ddgs()` 中显式传参：

```python
def _ddgs(self):
    return DDGS(proxy=self._resolve_proxy())
```

优先级 `DDGS_PROXY` > `HTTPS_PROXY` > `HTTP_PROXY`（大小写变体都覆盖），
空串视为未配置（空串会被 ddgs 当 URL 解析而报错）。

**真实价值（不是「修了一个 bug」）**：
1. 支持 `DDGS_PROXY` —— `primp` 不认它；
2. 代理来源在代码里可见，排查时不必猜底层客户端读到了什么。

**不承诺**：它不改变「ddgs 在本网络取不到结果」这一事实（第二、三层独立存在）。

## 6. 顺带踩到的 Windows 坑

写测试时 `monkeypatch.setenv("HTTP_PROXY", ...)` 之后又
`monkeypatch.delenv("http_proxy", raising=False)`，把刚设的值删掉了：
**Windows 的环境变量名大小写不敏感**，两者是同一个变量。已改为先清完再设值。

## 7. 验证

| 项 | 结果 |
| --- | --- |
| 后端全量 | **654 passed, 2 skipped** |
| 回归专项 | **116 passed, 7 deselected** |
| **人为漂移验证** | `_resolve_proxy` 不读任何变量 → `TestDDGSProxyWiring` **3 个断言失败**；还原后 34 passed |

## 8. 建议（按性价比排序）

1. **让 `.env` 的代理真正生效**：shell 里的 `HTTP_PROXY=4401` 会盖掉它。
   最稳的做法是在启动脚本里显式 export `.env` 的代理，或在 `config.py` 里
   用 `load_dotenv(override=True)`（⚠️ 后者会影响所有环境变量，需评估）。
2. **显式指定 `backend`**，把不可达的引擎排除掉，避免每次检索都去试一遍
   （`timeout` 默认 5s，多个不可用引擎累积起来就是十几秒）。
3. **配 `tavily_api_key`**（`api.tavily.com` 实测可达 200）或自建 `searxng_base_url`，
   让降级链真有第二个可降 —— 这是唯一能保证检索可用的措施。
4. 若代理支持，试试 **socks5**（官方文档明确支持），DDG 对 HTTP 代理可能不友好。
