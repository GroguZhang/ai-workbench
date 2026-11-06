---
name: gemini-api-probe
description: 实测 Google Gemini API 的真实可用性——验活 API Key、拉取该 Key 实际可调用的模型清单、探测 thinking 参数（thinkingLevel vs thinkingBudget）兼容性、区分「Key 无效 / 模型已下线 / 地域拦截 / 限流」四类失败。涉及 Gemini / Google AI Studio / generativelanguage.googleapis.com 的任何调试、报错排查、模型选型时使用。触发词：Gemini 报错, API Key 无效, User location is not supported, 模型下线, thinkingBudget, thinkingLevel, gemini-flash-latest, AI Studio Key。
agent_created: true
---

# Gemini API 探针

**核心原则：不要凭记忆猜 Gemini 的模型名和参数，一律实测。** 模型下线、新模型上线、参数改名都发生得很快，硬编码的记忆值通常在几个月内就失效。

## 关键事实（2026-09 实测，会过时，必须重测）

- `gemini-2.5-flash` / `gemini-2.5-flash-lite` / `2.0-flash` / `1.5-flash` 对**新用户已 404**：报 `no longer available to new users`。注意：`models.list` **仍会列出**这些模型，但调用必 404 —— **列得出来 ≠ 调得通**，必须实测调用。
- 主力 flash 系在售：`gemini-3.8-flash` / `3.7` / `3.5` / `3.5-flash-lite` / `3.1-flash-lite` / `3-flash-preview` / `gemini-flash-latest` / `gemini-flash-lite-latest` / `gemini-omni-flash-preview`。
- **`gemini-pro-latest` 在免费 Key 上直接 429 `RESOURCE_EXHAUSTED`**（无 pro 配额）。pro 系不要放进自动回退链。`gemini-2.5-pro` 同理。
- **503 `UNAVAILABLE`（high demand）会成片出现**：实测同一时刻 `flash-latest` / `3.5-flash` / `3.5-flash-lite` / `3-flash-preview` 集体 503，而 `3.8-flash`、`3.1-flash-lite` 正常。**这是 Google 侧的常态波动，不是配置错误** —— 所以「先固定一个模型再撞运气」的做法必然间歇性失败。
- 结论性设计原则：**发正式请求前先做「模型体检」**，逐个 ping 出当下真正可用的模型再干活（见下）。

### 自动挑选模型的正确姿势（已实测验证）

```
AUTO_CHAIN 顺序（旗舰 + 稳定兜底交替，保证 2 次内落地）：
  gemini-3.8-flash, gemini-3.1-flash-lite, gemini-3.5-flash, gemini-flash-latest,
  gemini-3.7-flash, gemini-3.5-flash-lite, gemini-3.1-flash-lite-preview,
  gemini-flash-lite-latest, gemini-3-flash-preview, gemini-omni-flash-preview
ping 请求体：{ generationConfig: { temperature: 0, maxOutputTokens: 4 } }，不带 thinkingConfig
止损规则（三条，缺一不可）：
  ① status 400 且含 "User location is not supported" → 立即停（IP 级封锁，后面 9 个一样失败）
  ② status 0（fetch 抛错）→ 立即停（网络层，全局性）
  ③ 前 6 个全是 503/429 → 停（整体拥堵）
```
**坑**：早期版本把止损阈值设成 5，且把稳定兜底模型排在链尾，结果在「3.8 系集体 503」时误停 —— 而 `3.1-flash-lite` 明明可用却还没被探到。阈值要比「兜底模型的位置」更靠后。

- `thinkingConfig` **两个字段都被接受**，但语义不同：
  - `{ "thinkingBudget": 0 }` → 可彻底关闭思考（实测 `thoughtsTokenCount: 0`），响应最快。
  - `{ "thinkingLevel": "high" }` → 开启深度思考（实测产生 thoughts token）。
  - 2.5 系列用 `thinkingBudget`；3.x 两者都行。**其它家族不要下发 thinkingConfig**，否则可能 400。
- 两种认证投递方式都有效，**不要只认一种**：
  - `?key=xxx` 查询参数
  - `x-goog-api-key: xxx` 请求头
  - 前端不应对 Key 前缀做白名单校验，格式由服务端裁定。
- `AQ.` 开头的 Key 实测可正常调用（`models.list` 与 `generateContent` 均 200）。不要假设只有 `AIza` 有效。

## 四类失败必须分清（决定排查方向完全不同）

| 现象 | HTTP / status | 真正原因 | 处理 |
|---|---|---|---|
| `API key not valid` / `API_KEY_INVALID` | 400 | 密钥本身无效/已吊销 | 换 Key |
| `no longer available to new users` | 404 | 模型下线 | 换模型 |
| `User location is not supported` | 400 `FAILED_PRECONDITION` | **出口 IP 地域被拦**，与 Key 无关 | 换受支持地区节点（日本/新加坡/美国）；国内直连、香港节点通常被拒 |
| `high demand` / `UNAVAILABLE` | 503 | 服务端临时过载 | 重试或换模型 |

**最容易误判的坑**：`models.list` 返回 200，**甚至同一出口 IP 会时而 200 时而报地域拦**（沙箱/共享 IP 池尤其明显）。列模型和实际生成走的是不同校验路径，**必须打一次真实生成探针**才能下结论。

## 第五类失败：浏览器端被 CSP 拦掉（curl 能通、页面报 Failed to fetch）

**症状**：`curl` 测同一 Key 全部 200，但页面里报 `TypeError: Failed to fetch` /「网络请求失败」。**没有 HTTP 状态码**——这是关键鉴别点，有状态码说明服务端已应答，没状态码说明请求压根没发出去。

**头号原因**：页面的执行环境下发了 `Content-Security-Policy`，其 `connect-src` 白名单不含 `generativelanguage.googleapis.com`。

- WorkBuddy **内置预览面板**实测就有这个问题。其响应头形如：
  ```
  Content-Security-Policy: default-src 'self' 'unsafe-inline' 'unsafe-eval' blob: data:;
    ... connect-src 'self' http://127.0.0.1:* https://*.map.qq.com https://*.gtimg.com
    https://*.app.workbuddy.link ...;
  ```
  白名单里**没有** Google API 域名，`fetch` 在发出前就被拦。查法：
  ```bash
  curl -s -I "<预览地址>" | grep -i content-security-policy
  ```
- **`frame-src`/iframe 不是重点，`connect-src` 才是**。CSP 拦截发生在发请求之前，任何 `mode` 都逃不掉。

**鉴别与解法**：
1. 看 `location.protocol`：`file://` 不受 CSP 约束 → 直接双击 HTML 打开即可。
2. 非 `file://` 时用**零成本可达性探针**区分「CSP 拦」和「网络不通」：
   ```js
   fetch(url, { method:'GET', mode:'no-cors', cache:'no-store' })  // 不消耗配额
   //  resolves → 请求真的发出去了（链路通）
   //  rejects(TypeError) → 被 CSP 拦掉或网络不通
   ```
   用 `mode:'no-cors'` 拿不到响应内容，但足以区分这两类。
3. 备选解法：用不带 CSP 头的裸静态服务，`python -m http.server 8000` 后访问
   `http://127.0.0.1:8000/index.html`（自己起的服务不设 CSP）。
4. `Content-Type: application/json` 会触发 CORS 预检；只要响应头 CSP 放行了 `connect-src`，
   预检本身 Google 是正常处理的，不必为此改 `text/plain`。


## 用法

```bash
bash <此 skill 目录>/probe.sh "你的KEY" [模型名]
```

脚本会依次输出：Key 是否被接受 → 该 Key 可调用的完整模型清单 → 真实生成探针结果 → SSE 流式连通性。不指定模型时默认探 `gemini-3.8-flash`。

## 沙箱环境注意

- 本机 curl 访问 Google HTTPS 需加 `--ssl-no-revoke`，否则证书吊销检查会失败。
- 沙箱出口 IP 可能被 Google 地域封锁（表现为 `User location is not supported`），此时 `models.list` 仍可能成功。**不要把沙箱的 400 地域错误当成代码 bug**——换本机带代理的环境验证。
- 写临时 JSON 文件时注意 Git Bash 的 `/tmp` 与 Node 看到的路径不一致，统一写到当前工作目录再用完即删。
