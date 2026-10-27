<p align="center">
  <img src="https://raw.githubusercontent.com/twitter/twemoji/master/assets/svg/1f510.svg" width="72" height="72" alt="polygate">
</p>
<h1 align="center">polygate</h1>
<p align="center">
  <b>国产大模型统一 API 网关</b> · <i>A unified OpenAI-compatible gateway for Chinese LLMs</i>
</p>
<p align="center">
  <img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT">
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB.svg?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/OpenAI-Compatible-412991.svg?logo=openai&logoColor=white" alt="OpenAI Compatible">
  <img src="https://img.shields.io/badge/FastAPI-0.110%2B-009688.svg?logo=fastapi&logoColor=white" alt="FastAPI">
  <a href="https://github.com/datouy/polygate/actions/workflows/ci.yml"><img src="https://github.com/datouy/polygate/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/github/stars/datouy/polygate?style=social" alt="GitHub stars">
</p>

---

> **一句话简介 / One-liner**：一把钥匙，接入 DeepSeek / 通义千问 / 智谱 GLM / Kimi / OpenAI —— OpenAI 兼容、多 Key 轮询、故障自动切换、限流熔断、成本记账、可观测、请求重放，以及面板内在线改配置（不改 yaml），开箱即用。
>
> *One key, every model. An OpenAI-compatible gateway for DeepSeek / Qwen / GLM / Kimi / OpenAI with multi-key rotation, automatic failover, rate limiting & circuit breaking, cost accounting, observability, request replay, and live config editing in the admin panel.*

---

## ✨ 简介 · Introduction

**中文**：在国内做 AI 应用，你往往要同时对接多家大模型：Key 格式、接口域名各不相同，单个 Key 有 QPS / 额度限制，某家服务抖动时还需要手动切备用家。**polygate** 把这些琐碎问题收敛成一个自部署的 OpenAI 兼容网关——你的代码只需指向 `http://127.0.0.1:8000/v1`，其余交给网关。

**English**: Building AI apps in China usually means juggling multiple LLM providers, each with different key formats and endpoints, per-key rate/quota limits, and the need to manually fail over when one provider wobbles. **polygate** collapses all of that into a single self-hosted, OpenAI-compatible gateway — point your code at `http://127.0.0.1:8000/v1` and forget the rest.

> 💡 可直接作为 GitHub 仓库的「About」简介（约一句话，中英各一）：
> - 中文：`国产大模型统一 API 网关：一把钥匙接入 DeepSeek/千问/GLM/Kimi/OpenAI，多 Key 轮询、故障切换、限流熔断、成本记账、可观测与重放。`
> - English: `A unified OpenAI-compatible gateway for Chinese LLMs (DeepSeek/Qwen/GLM/Kimi/OpenAI) with multi-key rotation, failover, rate limiting, cost accounting and observability.`

---

## 🚀 特性 · Features

| 特性 Feature | 说明 Description |
|---|---|
| 多 Provider 统一 · Multi-provider | DeepSeek / Qwen / GLM / Kimi / OpenAI，皆走 OpenAI 兼容协议 |
| 多 Key 轮询 · Key rotation | 同一 Provider 配多个 Key 自动轮换；401/403/429 自动换下一个 Key |
| 权重负载均衡 · Weighted LB | 同一模型配多个 Provider，按 weight 加权随机 |
| 故障自动切换 · Failover | 连接错误 / 5xx 换 Provider；400/404/422 请求本身问题不重试 |
| 流式透传 · Streaming | SSE 原样转发，自动注入 `stream_options.include_usage`（可用 `stream_include_usage` 开关） |
| Embeddings 转发 · Embeddings | `/v1/embeddings` 统一转发 |
| 语音转写转发 · Transcription | `/v1/audio/transcriptions` 统一转发（multipart） |
| 令牌桶限流 · Rate limiting | 按 Provider + Key 维度（内存 / Redis 分布式） |
| 熔断器 · Circuit breaker | 连续失败自动熔断，冷却后半开放行单个探测，自动恢复 |
| 用量统计 · Usage stats | 按模型/Provider 记录调用数、错误数、token、延迟 |
| SQLite 持久化 · Persistence | 每次调用落库，支持按日汇总 |
| 请求日志与重放 · Request replay | 列表/详情/一键重放，便于回归与排障 |
| 按 Key 成本记账 · Cost accounting | 结合各家 token 单价，按 Key/模型汇总成本 |
| Prometheus 指标 · Metrics | `/metrics` 导出调用、错误、token、延迟、熔断状态 |
| 排障透传 · Observability | 透传上游 `ratelimit-*` / `retry-after`，响应头带 `X-Request-ID` 可对齐请求日志 |
| Web 管理面板 · Admin panel | `/admin` 实时查看 Provider、模型、用量、成本、最近请求 |
| 在线改配置 · Live config | 面板内直接改 Provider 的 base_url / API Key，存 SQLite（**不改 config.yaml**），保存即热重载 |
| 网关鉴权 · Gateway auth | 用你自己的 Key 保护入口，不暴露上游 Key |

---

## 🛠 快速开始 · Quick Start

```bash
# 1. 安装依赖（建议用虚拟环境）· Install deps (venv recommended)
python -m venv .venv
.venv\Scripts\activate        # Windows；macOS/Linux 用 source .venv/bin/activate
pip install -r requirements.txt
# 可选：启用 Redis 分布式限流时再装（或 pip install "polygate[redis]"）
# pip install -r requirements-redis.txt

# 2. 准备配置 · Prepare config
copy config.example.yaml config.yaml    # Windows
cp config.example.yaml config.yaml      # macOS/Linux
# 编辑 config.yaml，填入你的 Key（或用环境变量 ${DEEPSEEK_API_KEY} 等）
# Edit config.yaml and fill in your keys (or use env vars like ${DEEPSEEK_API_KEY})

# 3. 启动 · Run
python run.py
# 或指定端口 / 配置 · or custom port/config
POLYGATE_PORT=8000 POLYGATE_CONFIG=config.yaml python run.py
```

打开 `http://127.0.0.1:8000/docs` 查看交互式 API 文档。· Open `http://127.0.0.1:8000/docs` for the interactive API docs.

> 💡 配置加载顺序：`POLYGATE_CONFIG` 环境变量 → `config.yaml`（存在则优先）→ `config.example.yaml`。
> 也可以启动后打开 `http://127.0.0.1:8000/admin`，在面板里**在线改 URL / Key**（存 SQLite，不改 `config.yaml`，保存即生效）。

### 🐳 Docker

```bash
# 1. 准备 config.yaml（参考 config.example.yaml），Key 可用环境变量 ${NAME} 引用
cp config.example.yaml config.yaml

# 2. 构建并启动
docker compose up -d --build
```

网关默认监听 `http://127.0.0.1:8000`；`config.yaml` 与数据目录 `./data` 通过卷挂载进容器。

---

## ⚙️ 配置 · Configuration

`config.example.yaml` 结构 · Structure：

```yaml
db_path: polygate.db             # SQLite：用量持久化 + 配置覆盖（留空则禁用）· persistence & config overrides
stream_include_usage: true       # 流式自动注入 include_usage 统计 token（部分厂商不兼容可关）· stream usage injection
auth:
  api_key: "change-me"           # 网关入口密钥 · gateway key
  admin_key: ""                  # 管理端口密钥（留空复用 api_key）· admin key

health:                          # 健康检查与熔断 · health & circuit breaker
  enabled: true
  interval: 30                   # 检查间隔（秒）· interval (s)
  failure_threshold: 3           # 连续失败 N 次后熔断 · trip after N failures
  cooldown: 60                   # 熔断冷却（秒）· cooldown (s)

# redis:                          # 可选：Redis 分布式限流 · optional distributed rate limiting
#   url: redis://127.0.0.1:6379/0

pricing:                          # 成本定价（每百万 token 单价）· price per 1M tokens
  deepseek-chat: { input: 2, output: 8 }
  "*": { input: 0, output: 0 }    # 兜底默认价 · fallback price

providers:
  deepseek:
    base_url: https://api.deepseek.com
    api_key: ${DEEPSEEK_API_KEY}   # 支持环境变量 · env var supported
    api_keys: []                    # 多 Key 轮询（可选）· extra keys
    rate_limit: { rate: 10, burst: 20 }   # 令牌桶限流（可选）· rate limiting

models:
  - name: deepseek-chat           # 对外暴露的模型名 · exposed model name
    providers:
      - { provider: deepseek, model: deepseek-chat, weight: 1 }

  - name: chat-fallback           # 故障切换示例 · failover example
    providers:
      - { provider: deepseek, model: deepseek-chat, weight: 3 }
      - { provider: qwen, model: qwen-plus, weight: 1 }

  - name: qwen-text-embedding-v3  # Embeddings 模型 · kind: embeddings
    kind: embeddings
    providers:
      - { provider: qwen, model: text-embedding-v3 }

  - name: whisper-1               # 语音转写模型 · kind: audio
    kind: audio
    providers:
      - { provider: openai, model: whisper-1 }
```

> 📝 **配置优先级**：`config.yaml`（模板，只读）→ 叠加 SQLite 覆盖（`polygate.db`，管理面板写入）。<br>
> 面板里改的 URL / Key **只进 SQLite，不写回 config.yaml**；想恢复 yaml 原值，点面板「重置为 yaml 配置」或调 `DELETE /v1/config`。

### 各 Provider 官方 OpenAI 兼容地址 · Provider base URLs

| 厂商 Provider | base_url |
|---|---|
| DeepSeek | `https://api.deepseek.com` |
| 通义千问 DashScope · Qwen | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| 智谱 GLM · Zhipu | `https://open.bigmodel.cn/api/paas/v4` |
| Moonshot Kimi | `https://api.moonshot.cn/v1` |
| OpenAI | `https://api.openai.com/v1` |
| 任意 OpenAI 兼容服务（vLLM/Ollama/one-api 等） | 自定义 · custom |

---

## 🖥 管理面板 · Admin Panel

启动后打开 `http://127.0.0.1:8000/admin`（默认口令 `change-me`，生产请改 `auth.admin_key`）：

| 区域 | 能做什么 |
|---|---|
| Providers | 查看熔断状态、连续失败次数、Key 数、限流配置 |
| Models | 查看「模型 → Provider」的路由映射 |
| 实时用量 / 每日历史 | 调用数、错误数、token、延迟 |
| 成本 | 按 Key / 模型汇总成本 |
| 最近请求 | 端点、模型、状态、延迟（可配合 API 重放） |
| **⚙️ 配置** | **改每个 Provider 的 base_url / API Key；新增 Provider（含官方预设）；保存即热重载；支持一键重置回 yaml** |

> 🔒 Key 永远只显示脱敏值（如 `c58d...f9db`），面板和接口都不会返回明文。

---

## 🔌 API

所有对话接口与 OpenAI 完全一致，把 `base_url` 指向网关即可。· All chat endpoints are OpenAI-compatible; just point `base_url` at the gateway.

| 方法 Method | 路径 Path | 说明 Description |
|---|---|---|
| GET | `/healthz` | 健康检查 · health check |
| GET | `/readyz` | 就绪检查（至少一个可用 Provider Key）· readiness |
| GET | `/v1/models` | 列出 chat 模型（embeddings / audio 不混入）· list chat models |
| POST | `/v1/chat/completions` | 对话 · chat（支持 `stream: true`） |
| POST | `/v1/completions` | 旧版补全 · legacy completion（自动转 chat） |
| POST | `/v1/embeddings` | 向量化 · embeddings |
| POST | `/v1/audio/transcriptions` | 语音转写 · transcription |
| GET | `/v1/status` | Provider 熔断/限流状态 · circuit/rate state（admin） |
| GET | `/v1/config` | 读取可编辑配置，Key 脱敏 · editable config, keys masked（admin） |
| PUT | `/v1/config` | 改 base_url / api_key → 存 SQLite + 热重载 · update & hot-reload（admin） |
| DELETE | `/v1/config` | 清除 SQLite 覆盖，回到 config.yaml · reset to yaml（admin） |
| POST | `/v1/config/reload` | 重新读 config.yaml 再叠加覆盖 · reload from file（admin） |
| GET | `/v1/usage` | 实时用量 · live usage（admin） |
| GET | `/v1/usage/history?days=7` | 每日用量历史 · daily history（admin） |
| GET | `/v1/costs` | 按 Key/模型成本 · cost summary（admin） |
| GET | `/v1/requests?limit=50` | 请求日志列表 · request log（admin） |
| GET | `/v1/requests/{req_id}` | 请求详情 · request detail（admin） |
| POST | `/v1/requests/{req_id}/replay` | 重放请求 · replay（admin） |
| GET | `/metrics` | Prometheus 指标 · metrics |
| GET | `/admin` | Web 管理面板 · admin panel |

### 用 openai 官方 SDK 调用 · Use the official OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="change-me",          # 网关密钥，不是上游 Key · gateway key, not upstream key
)
resp = client.chat.completions.create(
    model="deepseek-chat",        # 用配置里的模型名 · model name from config
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

### curl 调用 · curl

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer change-me" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"你好"}]}'
```

---

## 📁 目录结构 · Project Structure

```
polygate/
├── run.py                 # 启动入口 · entrypoint
├── pyproject.toml         # 打包 + ruff/mypy/pytest 配置 · packaging & tool config
├── requirements.txt       # 运行依赖 · runtime deps
├── requirements-redis.txt # 可选 Redis 依赖 · optional Redis deps
├── requirements-dev.txt   # 开发/测试依赖 · dev deps
├── config.example.yaml    # 配置示例 · example config
├── Dockerfile             # 容器镜像 · container image
├── docker-compose.yml     # 一键启动 · compose
├── CHANGELOG.md           # 变更记录 · changelog
├── CONTRIBUTING.md        # 贡献指南 · contributing guide
├── .github/
│   ├── workflows/ci.yml   # CI：pytest + ruff + Docker · CI pipeline
│   ├── ISSUE_TEMPLATE/    # Issue 模板 · issue templates
│   └── PULL_REQUEST_TEMPLATE.md
├── polygate/
│   ├── server.py          # FastAPI 应用与路由 · app & routes
│   ├── runtime.py         # 运行时状态 + 热重载 · runtime state & hot reload
│   ├── configstore.py     # 配置覆盖存 SQLite（不改 yaml）· config overrides
│   ├── config.py          # 配置加载 + 环境变量展开 · config loader
│   ├── db.py              # 单连接 + 后台批量写 · shared SQLite & batch writer
│   ├── providers.py       # Provider 抽象 · provider abstraction
│   ├── router.py          # 权重路由 / 多 Key 轮询 / 故障切换候选 · routing
│   ├── stats.py           # 内存用量统计 · in-memory stats
│   ├── store.py           # SQLite 用量持久化 · usage persistence
│   ├── requestlog.py      # 请求日志与重放 · request log & replay
│   ├── cost.py            # 成本记账 · cost accounting
│   ├── metrics.py         # Prometheus 指标 · metrics
│   ├── ratelimit.py       # 内存令牌桶限流 · in-memory rate limiting
│   ├── redislimiter.py    # Redis 分布式限流 · distributed rate limiting
│   ├── circuit.py         # 熔断器 · circuit breaker
│   ├── admin.py           # Web 管理面板 · admin panel
│   ├── schemas.py         # 请求模型 · request schemas
│   └── errors.py          # 异常与重试策略 · errors & retry policy
├── examples/
│   ├── client.py          # Python 客户端示例 · client example
│   └── curl.sh            # curl 示例 · curl example
└── tests/                 # 测试 · tests
    ├── test_core.py         # 路由 / 配置展开 / 统计 / 重试策略
    ├── test_ratelimit.py    # 内存令牌桶限流
    ├── test_redislimiter.py # Redis 限流（不可用时自动跳过）
    ├── test_circuit.py      # 熔断三态迁移 + 探测语义
    ├── test_store.py        # SQLite 用量持久化
    ├── test_metrics.py      # Prometheus 指标
    ├── test_cost.py         # 成本记账
    ├── test_requestlog.py   # 请求日志 / 超长响应截断
    ├── test_config_api.py   # 在线配置读写 / 热重载 / yaml 不变
    ├── test_failover.py     # 多 Key 401 轮询 / 别名重放
    └── test_e2e.py          # 端到端（Mock 上游）
```

---

## 🧪 测试 · Tests

```bash
pip install -e ".[dev]"
python -m pytest -q
```

覆盖：配置展开、路由/故障切换、多 Key 401 轮询、限流（内存/Redis）、熔断三态与健康探测语义、SQLite 持久化、Prometheus 指标、成本记账、请求日志与超长响应截断、**在线配置读写与热重载（SQLite 覆盖、yaml 不变）**，以及 Mock 上游下的非流式 + 流式 + Embeddings + 语音转写端到端流程与模型别名重放。

测试结果由 CI 动态生成（见上方徽章）；Redis 相关用例在本机未装/未启动 Redis 时自动跳过。

*Covers: config expansion, routing/failover, multi-key 401 rotation, rate limiting (memory/Redis), circuit-breaker states & probe semantics, SQLite persistence, Prometheus metrics, cost accounting, request logging & oversized-response truncation, live config editing with hot reload (SQLite overrides, yaml untouched), end-to-end chat (streaming & non-streaming), embeddings, transcription and model-alias replay against a mock upstream.*

---

## 🗺 路线图 · Roadmap

- [x] 持久化用量统计 · SQLite persistence
- [x] 限流 · Rate limiting（内存/Redis）
- [x] 健康检查与熔断 · Health check & circuit breaker
- [x] `/v1/embeddings` 转发 · Embeddings
- [x] Web 管理面板 · Admin panel
- [x] 请求日志与重放 · Request log & replay
- [x] 按 Key 成本记账 · Cost accounting
- [x] Prometheus 指标 · Metrics
- [x] `/v1/audio/transcriptions` 转发 · Transcription
- [x] Redis 分布式限流 · Distributed rate limiting
- [x] 管理面板在线改配置（SQLite 覆盖，yaml 只读）· Live config editing
- [x] Docker 镜像与 docker-compose · Docker image & compose
- [ ] 请求日志保留天数自动清理 · log retention cleanup
- [ ] 更多语音接口（翻译/tts）· more audio endpoints (translation/tts)

---

## 📋 变更记录 · Changelog

完整变更记录见 [CHANGELOG.md](CHANGELOG.md)；贡献方式见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## 📄 License

[MIT](LICENSE)
