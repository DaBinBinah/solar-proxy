# solar-proxy

**把 Upstage Solar 模型反代成 Anthropic API，直接给 Claude Code 用。**
**Use Upstage Solar models (solar-pro4) in Claude Code via a single-file Anthropic↔OpenAI proxy.**

```
Claude Code (Anthropic 协议) → solar-proxy (127.0.0.1:8787) → Upstage API (OpenAI 协议)
```

Claude Code 以为自己在和 `claude-sonnet-4-5` 说话，实际背后跑的是 `solar-pro4`。

---

## 为什么有这个项目

把 Claude Code 接到 OpenAI 兼容上游的方案不少（[claude-code-router](https://github.com/musistudio/claude-code-router)、[LiteLLM](https://github.com/BerriAI/litellm)、[anthropic-proxy](https://github.com/maxnowack/anthropic-proxy) 等），但大多数是 Node/Docker 多组件全家桶。solar-proxy 的取舍是：

- **单文件**：一个 `proxy.py`（约 400 行），能读懂、能改
- **零安装**：Python 3.9+ 标准库 + `requests`，没有 npm / Docker / 数据库
- **针对 Upstage 实测调优**：下面这些坑全部是在真实 Claude Code 多轮 agent 任务中踩出来并修掉的
- **开箱即用的 cc-switch 集成**：一条 SQLite 插入语句就能注册成切换项

适合：想在 Claude Code 里用 Upstage Solar 省钱的个人开发者。
不适合：需要多供应商路由、负载均衡、计费统计的重度团队场景（请用上面的全家桶）。

## 实测踩平的坑

这些不是理论问题，是 Claude Code 真实多轮工具调用中逐一复现并修复的：

1. **流式开关透传** —— Anthropic 请求里的 `stream` 必须显式传给上游，否则上游按非流式返回一整条 JSON，SSE 解析静默全空
2. **SSE 中文乱码** —— `text/event-stream` 不带 charset 时，requests 默认按 latin-1 解码，中文全成摩尔码
3. **带查询串的路径** —— Claude Code 实际请求 `/v1/messages?beta=true`，`endswith("/messages")` 会匹配失败
4. **工具调用历史回放错乱** —— assistant 历史消息的 `content: null` 和非标准 `toolu_*` id 会让 Solar 的对话模板状态错乱，第二轮起工具调用以文本形式泄露（`<|tool_call:start|>` 直接吐成正文）。修复：空串替代 null + id 重写为 `call_*` 格式

## 功能

- ✅ 流式 / 非流式（完整 Anthropic SSE 事件序列：`message_start` → `content_block_*` → `message_delta` → `message_stop`）
- ✅ function calling 双向转换（`tools` / `tool_use` / `tool_result`），Claude Code 多轮 agent 任务实测通过
- ✅ 模型映射：`claude-*haiku*` → `solar-mini4`（轻活），其余 → `solar-pro4`（重活），可用环境变量覆盖
- ✅ `thinking` 预算 → `reasoning_effort`（low/medium/high）自动映射
- ✅ `/v1/messages/count_tokens` 估算、`GET /v1/models`（Claude Code 启动校验需要）
- ✅ 工具调用往返延迟低（实测单轮 2-6s，含上游推理时间）

## 快速开始

```bash
git clone https://github.com/DaBinBinah/solar-proxy.git
cd solar-proxy
pip install -r requirements.txt

cp .env.example .env
# 编辑 .env，填入你的 Upstage key（https://console.upstage.ai 获取）

./start.sh
# {"status": "ok", ...} 即启动成功
```

### 接入 Claude Code（一次性测试）

```bash
claude -p "你是什么模型" --settings claude_settings.json
# → 我是 Solar Pro 4，由韩国 Upstage AI 开发的模型。
```

### 接入 cc-switch（推荐，一键切换）

在 cc-switch 新增自定义供应商，`claude_settings.json` 里的 env 块照抄即可：

| 键 | 值 |
|---|---|
| `ANTHROPIC_BASE_URL` | `http://127.0.0.1:8787` |
| `ANTHROPIC_AUTH_TOKEN` | `local-proxy`（任意非空） |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-5`（名字必须是 Claude Code 认识的，代理负责映射） |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | `claude-haiku-4-5` |

之后在 cc-switch 里点击切换，Claude Code 重启会话即生效；切回原供应商同样一键。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `UPSTAGE_API_KEY` | （必填） | Upstage 控制台获取 |
| `PORT` | `8787` | 监听端口 |
| `BIG_MODEL` | `solar-pro4` | 主模型（sonnet/opus 槽位） |
| `SMALL_MODEL` | `solar-mini4` | 轻量模型（haiku 槽位） |
| `UPSTAGE_BASE_URL` | `https://api.upstage.ai/v1` | 上游地址（理论上可指向任意 OpenAI 兼容上游） |

## 工作原理

```
Claude Code                    solar-proxy                     Upstage
    │  POST /v1/messages?beta=true   │                              │
    │  (Anthropic Messages 格式)     │                              │
    │ ─────────────────────────────► │  POST /chat/completions      │
    │                                │  (OpenAI 格式 + stream)      │
    │                                │ ────────────────────────────►│
    │                                │  SSE: chat.completion.chunk  │
    │  SSE: message_start /          │◄────────────────────────────│
    │       content_block_delta /    │  逐块转换（文本/工具调用/用量）│
    │       input_json_delta ...     │                              │
    │◄───────────────────────────── │                              │
```

核心转换逻辑（`proxy.py`）：

- `convert_request()`：system 合并、`tool_use`→`tool_calls`、`tool_result`→`role:"tool"`、工具 id 重写、thinking→reasoning_effort
- `StreamTransformer`：OpenAI chunk 流 → Anthropic SSE 事件流，文本块与工具调用块按序编号
- `stop_reason` 映射：`tool_calls`→`tool_use`、`length`→`max_tokens`、其余→`end_turn`

## 已知限制

- Solar 是**纯文本模型**：Claude Code 里的图片/截图输入会被丢弃
- 上游上下文窗口比 Claude 小，超长会话会收到上游报错（代理原样透传，方便定位）
- `count_tokens` 是字符估算值（中文约 0.7 token/字 + 每请求固定开销），非精确计量

## 同类项目

| 项目 | 特点 |
|---|---|
| [claude-code-router](https://github.com/musistudio/claude-code-router) | 功能最全的多模型路由器（Node） |
| [LiteLLM](https://github.com/BerriAI/litellm) | 企业级网关，100+ 提供商（Python，重） |
| [anthropic-proxy](https://github.com/maxnowack/anthropic-proxy) | 同类协议转换（Node/TypeScript） |
| **solar-proxy** | **单文件零依赖，专为 Upstage Solar 实测调优** |

## 免责声明

本项目为个人开发的第三方工具，与 Upstage AI、Anthropic 均无关联。
请遵守各服务商的服务条款；因使用本工具产生的账号风险与费用由使用者自行承担。

## License

[MIT](LICENSE) © 2026 DaBinBinah

如果对你有用，欢迎 ⭐ Star —— 这是我第六个开源项目 🎉
