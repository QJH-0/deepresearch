# 排查：ddgs 为什么超时——「我配了代理」为什么没生效

- 日期：2026-09-19
- 触发：用户提问「ddgs 为什么会链接超时，我不是配置了代理吗」
- 结论：**配了，但 ddgs 用不上；而且即便用上，ddgs 在本网络仍不可用**

## 先确认：代理本身是好的

`.env` 配的是 `HTTP_PROXY=http://127.0.0.1:7897`。实测走它：

| 目标 | 结果 |
| --- | --- |
| www.google.com | **302**（1.1s） |
| www.bing.com/search | **200**（0.87s） |
| en.wikipedia.org | **301**（1.9s） |
| api.tavily.com | **200**（3.5s） |
| **duckduckgo.com / html.duckduckgo.com** | **超时** |

代理没问题，**唯独 DuckDuckGo 走不通**。

## 三层原因，逐层剥开

### 第一层：ddgs 不读 `HTTP_PROXY`

ddgs 源码 `ddgs/api.py`：

```python
def DDGS_from_env():   # 注释：Create a DDGS instance with proxy configuration from environment.
    return DDGS(proxy=_expand_proxy_tb_alias(os.environ.get("DDGS_PROXY")))
```

**它只认 `DDGS_PROXY` 或构造参数 `proxy=`，不读通用的 `HTTP_PROXY`。**
而项目的 `DuckDuckGoProvider._ddgs()` 原本写的是 `return DDGS()` —— 代理完全没生效。

**验证**：把代理指向一个没人监听的端口（`http://127.0.0.1:9`），ddgs 仍是
**慢超时 12.3s** 而非快速连接被拒 —— 说明它压根没走代理。

### 第二层：shell 里的 `HTTP_PROXY` 会盖掉 `.env`

```
shell  : HTTP_PROXY=http://127.0.0.1:4401   （WorkBuddy 注入）
.env   : HTTP_PROXY=http://127.0.0.1:7897
```

`load_dotenv` **默认不覆盖已存在的环境变量**（`.env` 里就写着这条警告），
于是 4401 赢。而实测走 4401 访问 google **超时（curl 退出码 124）** ——
4401 是给 agent 自身工具用的，不是通用出口代理。

所以直接跑评测会全程用坏代理；我此前跑评测时加 `env -u HTTP_PROXY -u HTTPS_PROXY ...`
正是为了绕开它（让 `.env` 的 7897 生效）。

### 第三层：即使把好的代理传给 ddgs，各后端仍不通

| 配置 | 结果 |
| --- | --- |
| 无代理 | TimeoutException 16.7s |
| `proxy=7897` | DDGSException 14.8s |
| `proxy=7897 + backend=bing` | TimeoutException 12.5s |
| `proxy=7897 + backend=brave` | DDGSException **0.9s** |

brave 变成 0.9s 快速失败（且 curl 走代理到 brave 是 **429 限流**）说明
**代理确实被用上了**；但 duckduckgo 与 bing 经 ddgs 仍不通。

底层 `primp`（ddgs 的 HTTP 客户端）本身没问题：直连和走代理访问 bing 都是 **200 / 0.4s**。
所以问题在 ddgs 访问的具体端点与其请求构造，不在网络栈。

## 与「评测对照失效」的关系

`search_providers = ["ddgs", "tavily", "searxng"]`，但 `tavily_api_key` 与
`searxng_base_url` **都没配** —— 降级链上只有 ddgs 一个真在跑，而它不可用。
**全链失败**，这才是 baseline 一条 web 证据都拿不到的根因。

好消息：**`api.tavily.com` 实测可达（200）**，配个 tavily key 即可恢复检索。

## 本次修复

`DuckDuckGoProvider` 新增 `_resolve_proxy()` 并在 `_ddgs()` 中显式传参：

```python
def _ddgs(self):
    return DDGS(proxy=self._resolve_proxy())
```

优先级 `DDGS_PROXY` > `HTTPS_PROXY` > `HTTP_PROXY`（大小写变体都覆盖）；
空串视为未配置（空串会被 ddgs 当 URL 解析而报错）。

**收益**：让「源不可达」从 16s 慢超时变成快速失败，不再拖慢整条检索链。
**不承诺**：它不能救活 ddgs —— 第三层问题独立存在。

## 顺带踩到的 Windows 坑

写测试时 `monkeypatch.setenv("HTTP_PROXY", ...)` 之后又
`monkeypatch.delenv("http_proxy", raising=False)`，结果把刚设的值删掉了：
**Windows 的环境变量名大小写不敏感**，两者是同一个变量。已改为先清完再设值，
并在测试里注明。

## 验证

| 项 | 结果 |
| --- | --- |
| 后端全量 | **654 passed, 2 skipped** |
| 回归专项 | **116 passed, 7 deselected** |
| **人为漂移验证** | `_resolve_proxy` 不读任何变量 → `TestDDGSProxyWiring` **3 个断言失败**；还原后 34 passed |

## 建议

1. **配 `tavily_api_key`**（`api.tavily.com` 实测可达）—— 最直接的修法
2. 或自建 `searxng_base_url`，让降级链真正有第二个可降
3. ddgs 在本网络下不可用，**不宜继续作为首选 Provider**；可考虑调整
   `search_providers` 顺序，把可用的源放前面
4. 评测/线上运行时注意 `HTTP_PROXY` 被 shell 覆盖的问题
   （`.env` 的注释已警告，可考虑在启动时显式校验代理可达性）
