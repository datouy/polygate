# polygate

国产大模型统一 API 网关。对外提供一个 OpenAI 兼容端点，统一处理多 Provider 路由、故障切换、限流、配额、成本和管理面板。

[快速开始](#快速开始--quick-start) · [管理面板](#管理面板--admin-panel) · [API](#api) · [变更记录](CHANGELOG.md)

---

## 功能 · Features

| 特性 Feature | 说明 Description |
|---|---|
| 多 Provider 统一 · Multi-provider | DeepSeek / Qwen / GLM / Kimi / OpenAI，皆走 OpenAI 兼容协议 |
| 多 Key 轮询 · Key rotation | 同一 Provider 配多个 Key 自动轮换；401/403/429 自动换下一个 Key |
| 权重负载均衡 · Weighted LB | 同一模型配多个 Provider，按 weight 加权随机 |
| 故障自动切换 · Failover | 连接错误 / 5xx 换 Provider；400/404/422 请求本身问题不重试 |
| 请求总超时/重试预算 · Request budget | 单次逻辑请求可设总超时、最大候选数与 429/401 退避，避免全挂时长期占用连接 |
| 原子热重载 · Atomic hot reload | 配置先构建成不可变快照再一次性切换；构建失败保留旧配置、不写 SQLite |
| 配额预占 · Quota reservation | 日/月调用配额用进程内计数预约，请求热路径不重复扫库；成本预算另由 SQLite 租约账本控制 |
| 请求体大小限制 · Body limits | 普通请求 10MB / 音频 25MB，Content-Length 与 chunked/multipart 读取路径都拦截 |
| 流式透传 · Streaming | SSE 原样转发，自动注入 `stream_options.include_usage`（可用 `stream_include_usage` 开关） |
| Embeddings 转发 · Embeddings | `/v1/embeddings` 统一转发 |
| 语音转写转发 · Transcription | `/v1/audio/transcriptions` 统一转发（multipart） |
| 令牌桶限流 · Rate limiting | 按 Provider + Key 维度（内存 / Redis 分布式） |
| 熔断器 · Circuit breaker | 连续失败自动熔断，冷却后半开放行单个探测，自动恢复 |
| 用量统计 · Usage stats | 按模型/Provider 记录调用数、错误数、token、延迟 |
| SQLite 持久化 · Persistence | 每次调用落库，支持按日汇总 |
| 请求日志与重放 · Request replay | 列表/详情/一键重放，便于回归与排障 |
| 逻辑请求聚合 · Attempt groups | 将故障切换中的多次尝试合并为一条逻辑请求，保留完整 attempt 链 |
| 多模型重放实验室 · Replay lab | 同一历史请求依次重放到多个模型，对比 Provider、延迟、成本与响应 |
| Provider 兼容性矩阵 · Compatibility matrix | 主动探测对话、流式 usage、工具调用、JSON Mode 与 `/models` 端点，结果持久化 |
| Provider 保存前测试 · Pre-save test | 使用新地址与 Key 探测 `/models`，返回延迟、状态码和发现的模型，不写配置 |
| 能力约束式路由 · Capability routing | 控制器会从 tools、JSON Mode、图片输入和 stream 自动推导能力要求，只在兼容候选间切换 |
| 硬预算账本 · Budget ledger | 月预算在调用前按成本上限原子预占，调用后按 usage 结算；SQLite 多进程共享时仍保持原子检查 |
| 按 Key 成本记账 · Cost accounting | 结合各家 token 单价，按 Key/模型汇总成本 |
| Prometheus 指标 · Metrics | `/metrics` 导出调用、错误、token、延迟、熔断状态 |
| 排障透传 · Observability | 透传上游 `ratelimit-*` / `retry-after`，响应头带 `X-Request-ID` 可对齐请求日志 |
| Web 管理面板 · Admin panel | `/admin` 实时查看 Provider、模型、用量、成本、最近请求 |
| 在线改配置 · Live config | 面板内改 Provider / 模型 / 预算参数，存 SQLite（**不改 config.yaml**），保存即热重载 |
| 网关鉴权 · Gateway auth | 用你自己的 Key 保护入口，不暴露上游 Key |
| 加密密钥存储 · Encrypted secrets | Provider Key 在 SQLite 中加密；虚拟 Key 只存 SHA-256 哈希，明文仅在创建或轮换时返回一次 |
| 可审阅配置发布 · Reviewable config | revision / ETag 防止并发覆盖；先验证、查看 diff，再原子应用 |

---

## 快速开始 · Quick Start

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

> 🔐 生产环境请设置 `POLYGATE_MASTER_KEY` 为至少 32 字节的随机值。未设置时会自动生成 `polygate.db.master-key`；两者都能让现有数据库继续工作，但环境变量更适合容器和独立备份。

> 🧱 请求体大小限制（防内存被打满）：POLYGATE_MAX_REQUEST_BYTES 默认 10MB（JSON 请求），POLYGATE_MAX_AUDIO_BYTES 默认 25MB（音频上传）。超限直接返回 413；限制按实际读取字节数校验，chunked 编码同样生效。

### Docker

```bash
# 1. 准备 config.yaml（参考 config.example.yaml），Key 可用环境变量 ${NAME} 引用
cp config.example.yaml config.yaml

# 2. 构建并启动
docker compose up -d --build
```

网关默认监听 `http://127.0.0.1:8000`；`config.yaml` 与数据目录 `./data` 通过卷挂载进容器。

---

## 配置 · Configuration

`config.example.yaml` 结构 · Structure：

```yaml
db_path: polygate.db             # SQLite：用量持久化 + 配置覆盖（留空则禁用）· persistence & config overrides
stream_include_usage: true       # 流式自动注入 include_usage 统计 token（部分厂商不兼容可关）· stream usage injection
log_retention_days: 0            # 日志保留天数（0=永久）；>0 定期清理过旧记录，防止 SQLite 无限增长 · log retention
limits:                          # 单次逻辑请求的容错预算 · per-request fault-tolerance budget
  total_timeout: 180             # 整个请求（含故障切换重试）的总超时（秒）· total timeout (s)
  max_attempts: 8                # 最多尝试的 Provider × Key 候选数 · max upstream attempts
  retry_backoff: 0.15            # 重试基础退避（秒），带随机抖动，指数封顶 · retry backoff base (s)
budget:                          # 客户月预算硬预占 · hard monthly budget reservations
  default_max_output_tokens: 4096
  bytes_per_token: 2.0           # 估算宽度，越小越保守 · smaller values reserve more
  reservation_ttl: 900           # 异常中断预占失效时间（秒）· stale reservation TTL
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
      - { provider: deepseek, model: deepseek-chat, weight: 3, capabilities: [stream, tools, json] }
      - { provider: qwen, model: qwen-plus, weight: 1, capabilities: [stream, tools] }

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
>
> Provider Key 在覆盖库中只保存加密密文和版本化引用。旧版本留在 `config_overrides` 中的明文会在启动时迁移；虚拟 Key 的旧 `auth.client_keys` 会迁移为哈希记录。

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

## 管理面板 · Admin Panel

启动后打开 `http://127.0.0.1:8000/admin`（默认口令 `change-me`，生产请改 `auth.admin_key`）：

| 区域 | 能做什么 |
|---|---|
| 总览 | Provider 健康、本进程请求与错误率、Token、近 30 天成本、7 天趋势、可跳转的待处理项 |
| 路由 | Provider 状态、密钥数量、连续失败、限流；新建或编辑模型、多上游权重与能力 |
| 用量与成本 | 模型用量、延迟、每日历史、按 Key / 客户端成本、客户端配额 |
| 请求 | 按逻辑请求聚合、过滤、分页、完整 attempt 链、请求和响应详情 |
| 诊断 | Provider 兼容性探针，确认后写入路由能力；多模型请求对比 |
| 设置 | Provider 测试与修改、新增 Provider、虚拟 Key 生命周期、预算参数 |

管理密钥保存在当前浏览器的 `localStorage`，页面不会把明文 Key 放在常驻输入框或接口响应中。生产环境请配置独立的 `auth.admin_key`，并在 `/admin` 前增加 HTTPS。

---

## API

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
| POST | `/v1/config/validate` | 验证配置并返回 diff，无副作用 · validate and diff（admin） |
| PUT | `/v1/config` | 改 Provider / 模型 / 预算参数 → 存 SQLite + 热重载 · update & hot-reload（admin） |
| DELETE | `/v1/config` | 清除 SQLite 覆盖，回到 config.yaml · reset to yaml（admin） |
| POST | `/v1/config/reload` | 重新读 config.yaml 再叠加覆盖 · reload from file（admin） |
| POST | `/v1/providers/{name}/test` | 测试未保存或现有 Provider 的地址与 Key · pre-save provider probe（admin） |
| GET | `/v1/client-keys` | 列出虚拟 Key 元数据，不返回明文或哈希 · list virtual keys（admin） |
| POST | `/v1/client-keys` | 创建虚拟 Key，明文只返回一次 · create virtual key（admin） |
| PATCH | `/v1/client-keys/{id}` | 修改名称、白名单、配额、预算和到期时间 · update virtual key（admin） |
| POST | `/v1/client-keys/{id}/rotate` | 轮换并立即废弃旧 Key · rotate virtual key（admin） |
| DELETE | `/v1/client-keys/{id}` | 软吊销虚拟 Key · revoke virtual key（admin） |
| GET | `/v1/usage` | 实时用量 · live usage（admin） |
| GET | `/v1/usage/history?days=7` | 每日用量历史 · daily history（admin） |
| GET | `/v1/costs` | 近 30 天按 Key / 模型 / 客户端成本 · rolling 30-day cost summary（admin） |
| GET | `/v1/requests?limit=50` | 请求日志列表 · request log（admin） |
| GET | `/v1/requests/{req_id}` | 请求详情 · request detail（admin） |
| GET | `/v1/requests/groups?limit=50` | 按逻辑请求聚合的列表 · logical request groups（admin） |
| GET | `/v1/requests/groups/{group_id}` | 完整 attempt 链与最终响应 · group detail（admin） |
| POST | `/v1/requests/{req_id}/replay` | 重放请求 · replay（admin） |
| POST | `/v1/replay-lab` | 同一请求多模型对比重放 · multi-model replay lab（admin） |
| GET | `/v1/compatibility` | 读取最近一次兼容性矩阵 · latest compatibility matrix（admin） |
| POST | `/v1/compatibility/run` | 运行指定探针 · run compatibility probes（admin） |
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

### 能力约束式路由 · Capability-constrained routing

在模型级或单个 Provider 路由上声明 `capabilities`，请求要求的能力必须是该路由能力的子集。模型级与路由级能力会合并：

```yaml
models:
  - name: chat-fallback
    capabilities: [stream]
    providers:
      - { provider: deepseek, model: deepseek-chat, capabilities: [tools, json] }
      - { provider: qwen, model: qwen-plus, capabilities: [tools] }
```

网关会自动识别 `stream`、工具调用、JSON Mode、图片输入及 audio modality。也可以显式要求：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer change-me" \
  -H "Content-Type: application/json" \
  -H "X-Polygate-Require: tools,json" \
  -d '{"model":"chat-fallback","messages":[{"role":"user","content":"查天气"}]}'
```

请求体也可传 `"polygate_requirements": ["tools", "json"]`。控制头与字段只供网关筛选，不会转发给上游。为兼容旧配置，**未声明 `capabilities` 的路由不会参与筛选**；一旦声明，就必须满足请求的全部能力要求。

### 重放实验室与兼容性矩阵 · Replay lab & compatibility matrix

重放实验室基于请求日志中的 `req_id`，依次调用多个公开模型，并返回 Provider、上游模型、延迟、usage、成本和响应或错误：

```bash
curl http://127.0.0.1:8000/v1/replay-lab \
  -H "Authorization: Bearer change-me" \
  -H "Content-Type: application/json" \
  -d '{"request_id":"历史 req_id","models":["deepseek-chat","qwen-plus"],"include_response":true}'
```

实验室最多接受 20 个目标公开模型，强制关闭流式并移除 `stream_options` 以拿到完整 JSON。它不能在同名 alias 内强制指定某个 Provider；Provider 级兼容性由矩阵探测：

```bash
curl -X POST http://127.0.0.1:8000/v1/compatibility/run \
  -H "Authorization: Bearer change-me" \
  -H "Content-Type: application/json" \
  -d '{"models":["chat-fallback"],"probes":["chat","stream_usage","tools","json"]}'
```

可用探针为 `chat`、`stream_usage`、`tools`、`json`、`models`；结果持久化后由 `GET /v1/compatibility` 读取。`stream_usage` 成功会归一化为路由能力 `stream`，`models` 只是 `/models` 端点诊断，不作为路由能力。矩阵一次最多执行 200 次上游调用；管理台可在确认 diff 后把成功结果并入对应路由的 `capabilities`。重放和兼容性探测都会产生**真实上游请求与费用**，并绕过虚拟 Key 的配额/预算限制；重放成本以 `replay-lab` 客户端名记账，矩阵属于管理员诊断，不进入虚拟 Key 预算账本。

### 预算账本 · Budget ledger

`auth.client_keys[].monthly_budget` 仍是每个业务方的预算上限。配置 `db_path` 后，预算会走 SQLite 租约账本：请求前按序列化请求体积、`bytes_per_token` 和最大输出 token 估算成本上限，使用 `BEGIN IMMEDIATE` 原子检查并预占；请求结束后按上游 usage 结算。上游未返回 usage 时，按预占上限保守结算。

该预占是**基于 pricing 的保守估算**，不是精确 tokenizer 上界，也不覆盖按分钟、图片、音频时长等非 token 计价。实际 usage 高于估算时仍可能超过预算，超出部分会计入已用成本。`reservation_ttl` 应大于单次请求的 `limits.total_timeout`，异常中断的租约到期后自动失效。若需要按 token 精确控制，请同时让上游返回 usage（流式建议保留 `stream_include_usage: true`）。

---

## 目录结构 · Project Structure

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
│   ├── virtualkeys.py     # 虚拟 Key 生命周期 / 哈希存储 · virtual key lifecycle
│   ├── secretstore.py     # Provider 密钥加密与迁移 · encrypted provider secrets
│   ├── cost.py            # 成本记账 · cost accounting
│   ├── budget.py          # 预算预占 / 结算账本 · budget reservation ledger
│   ├── compat.py          # Provider 兼容性结果存储 · compatibility results
│   ├── metrics.py         # Prometheus 指标 · metrics
│   ├── ratelimit.py       # 内存令牌桶限流 · in-memory rate limiting
│   ├── redislimiter.py    # Redis 分布式限流 · distributed rate limiting
│   ├── circuit.py         # 熔断器 · circuit breaker
│   ├── admin.py           # 管理面板资源入口 · console assets
│   ├── web/
│   │   ├── admin.html     # 管理面板结构 · console markup
│   │   ├── admin.css      # 管理面板样式 · console styles
│   │   └── admin.js       # 管理面板交互 · console behavior
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
    ├── test_provider_secrets.py # 密钥迁移 / 加密 / 错误主密钥
    ├── test_provider_test.py    # Provider 保存前测试
    ├── test_request_groups.py   # 逻辑请求与 attempt 链
    ├── test_virtualkeys.py      # 虚拟 Key 生命周期
    ├── test_failover.py     # 多 Key 401 轮询 / 别名重放
    └── test_e2e.py          # 端到端（Mock 上游）
```

---

## 测试 · Tests

```bash
pip install -e ".[dev]"
python -m pytest -q
```

覆盖：配置展开、路由/故障切换、多 Key 401 轮询、限流（内存/Redis）、熔断三态与健康探测语义、SQLite 持久化、Prometheus 指标、成本记账、请求日志与 attempt 聚合、Provider 密钥加密/迁移、虚拟 Key 生命周期、**在线配置验证、diff、revision 与热重载**，以及 Mock 上游下的非流式 + 流式 + Embeddings + 语音转写端到端流程与模型别名重放。

测试结果由 CI 动态生成（见上方徽章）；Redis 相关用例在本机未装/未启动 Redis 时自动跳过。

*Covers: config expansion, routing/failover, multi-key 401 rotation, rate limiting (memory/Redis), circuit-breaker states & probe semantics, SQLite persistence, Prometheus metrics, cost accounting, request logging & oversized-response truncation, live config editing with hot reload (SQLite overrides, yaml untouched), end-to-end chat (streaming & non-streaming), embeddings, transcription and model-alias replay against a mock upstream.*

---

## 路线图 · Roadmap

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
- [x] 请求日志保留天数自动清理 · log retention cleanup
- [x] 面板内编辑模型路由、权重与能力 · model route editor
- [x] 面板内创建、撤销和轮换虚拟 Key · virtual key lifecycle
- [x] Provider 保存前连通性测试与配置差异审阅 · connection test & config diff
- [ ] 更多语音接口（翻译/tts）· more audio endpoints (translation/tts)

---

## 变更记录 · Changelog

完整变更记录见 [CHANGELOG.md](CHANGELOG.md)；贡献方式见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## License

[MIT](LICENSE)
