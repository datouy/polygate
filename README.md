# polygate · 国产大模型统一 API 网关

> 一把钥匙，接入 DeepSeek / 通义千问 / 智谱 GLM / Kimi / OpenAI。
> 纯 Python + FastAPI，OpenAI 兼容，支持多 Key 轮询、权重负载均衡、故障自动切换、流式透传、
> 令牌桶限流（内存/Redis）、熔断器、SQLite 用量持久化、请求日志与重放、按 Key 成本记账、
> Embeddings / 语音转写转发、Prometheus 指标与内置管理面板。

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)

---

## 为什么需要它

在国内做 AI 应用，你往往要同时对接多家大模型：

- 每家 Key 格式、接口域名各不相同；
- 一个 Key 有 QPS / 额度限制，需要多 Key 分摊；
- 某家服务抖动时需要自动切到备用家；
- 想统一用 OpenAI SDK 调用，但各家兼容度参差。

**polygate 把这些问题收敛成一个自部署的 OpenAI 兼容网关**：你的代码只需要指向 `http://127.0.0.1:8000/v1`，其余交给网关。

| 能力 | 说明 |
|---|---|
| 多 Provider 统一 | DeepSeek / Qwen / GLM / Kimi / OpenAI，皆走 OpenAI 兼容协议 |
| 多 Key 轮询 | 同一 Provider 配多个 Key，自动轮换分摊 |
| 权重负载均衡 | 同一模型配多个 Provider，按 weight 加权随机 |
| 故障自动切换 | 连接错误 / 429 / 5xx 自动尝试下一个候选；4xx 不重试 |
| 流式透传 | SSE 原样转发，自动注入 `stream_options.include_usage` |
| Embeddings 转发 | `/v1/embeddings` 统一转发到各家的 embedding 模型 |
| 用量统计 | 按模型/Provider 记录调用数、错误数、token、延迟 |
| SQLite 持久化 | 每次调用落库，支持按日汇总查询 |
| 令牌桶限流 | 按 Provider + Key 维度限流（rate/burst） |
| 熔断器 | 连续失败自动熔断，冷却后半开放行单个探测，自动恢复 |
| 请求日志与重放 | 每次调用落库，支持列表/详情/一键重放 |
| 按 Key 成本记账 | 结合各家 token 单价，按 Key/模型统计成本 |
| Prometheus 指标 | `/metrics` 导出调用、错误、token、延迟、熔断状态 |
| 语音转写转发 | `/v1/audio/transcriptions` 统一转发（multipart） |
| Redis 分布式限流 | 多进程共享限流状态（可选，未配置则内存限流） |
| Web 管理面板 | `/admin` 实时查看 Provider、模型、用量、成本、最近请求 |
| 网关鉴权 | 用你自己的 Key 保护入口，不暴露上游 Key |

---

## 快速开始

```bash
# 1. 安装依赖（建议用虚拟环境）
python -m venv .venv
.venv\Scripts\activate        # Windows；macOS/Linux 用 source .venv/bin/activate
pip install -r requirements.txt

# 2. 准备配置
copy config.example.yaml config.yaml    # Windows
cp config.example.yaml config.yaml      # macOS/Linux
# 编辑 config.yaml，填入你的 Key（或用环境变量 ${DEEPSEEK_API_KEY} 等）

# 3. 启动
python run.py
# 或指定端口 / 配置
POLYGATE_PORT=8000 POLYGATE_CONFIG=config.yaml python run.py
```

打开 `http://127.0.0.1:8000/docs` 查看交互式 API 文档。

---

## 配置说明

`config.example.yaml` 结构：

```yaml
db_path: polygate.db             # SQLite 持久化（留空则禁用）
auth:
  api_key: "change-me"           # 网关入口密钥，客户端需带 Authorization: Bearer <此值>
  admin_key: ""                  # 管理端口密钥（留空复用 api_key）

health:                          # 健康检查与熔断
  enabled: true
  interval: 30                   # 检查间隔（秒）
  failure_threshold: 3           # 连续失败 N 次后熔断
  cooldown: 60                   # 熔断冷却（秒）

providers:
  deepseek:
    base_url: https://api.deepseek.com
    api_key: ${DEEPSEEK_API_KEY}   # 支持环境变量引用
    api_keys: []                    # 多 Key 轮询（可选）
    rate_limit: { rate: 10, burst: 20 }   # 令牌桶限流（可选）

models:
  - name: deepseek-chat           # 对外暴露的模型名
    providers:
      - { provider: deepseek, model: deepseek-chat, weight: 1 }

  - name: chat-fallback           # 故障切换示例
    providers:
      - { provider: deepseek, model: deepseek-chat, weight: 3 }
      - { provider: qwen, model: qwen-plus, weight: 1 }

  - name: qwen-text-embedding-v3  # Embeddings 模型
    kind: embeddings
    providers:
      - { provider: qwen, model: text-embedding-v3 }
```

### 各 Provider 官方 OpenAI 兼容地址

| 厂商 | base_url |
|---|---|
| DeepSeek | `https://api.deepseek.com` |
| 通义千问 DashScope | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` |
| Moonshot Kimi | `https://api.moonshot.cn/v1` |
| OpenAI | `https://api.openai.com/v1` |
| 任意 OpenAI 兼容服务（vLLM/Ollama/one-api 等） | 自定义 |

---

## API

所有对话接口与 OpenAI 完全一致，把 `base_url` 指向网关即可：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| GET | `/v1/models` | 列出配置的模型 |
| POST | `/v1/chat/completions` | 对话（支持 `stream: true`） |
| POST | `/v1/completions` | 旧版补全（自动转 chat） |
| POST | `/v1/embeddings` | 向量化（统一转发） |
| POST | `/v1/audio/transcriptions` | 语音转写（统一转发） |
| GET | `/v1/status` | Provider 熔断/限流状态（需 admin key） |
| GET | `/v1/usage` | 实时用量统计（需 admin key） |
| GET | `/v1/usage/history?days=7` | 每日用量历史（需 admin key） |
| GET | `/v1/costs` | 按 Key/模型成本汇总（需 admin key） |
| GET | `/v1/requests?limit=50` | 请求日志列表（需 admin key） |
| GET | `/v1/requests/{req_id}` | 请求详情（需 admin key） |
| POST | `/v1/requests/{req_id}/replay` | 重放请求（需 admin key） |
| GET | `/metrics` | Prometheus 指标 |
| GET | `/admin` | Web 管理面板 |

### 用 openai 官方 SDK 调用

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="change-me",          # 网关密钥，不是上游 Key
)
resp = client.chat.completions.create(
    model="deepseek-chat",        # 用配置里的模型名
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

### curl 调用

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer change-me" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"你好"}]}'
```

---

## 目录结构

```
polygate/
├── run.py                 # 启动入口
├── config.example.yaml    # 配置示例
├── requirements.txt
├── polygate/
│   ├── server.py          # FastAPI 应用与路由
│   ├── config.py          # 配置加载 + 环境变量展开
│   ├── providers.py       # Provider 抽象（chat/embeddings/models）
│   ├── router.py          # 权重路由 / 多 Key 轮询 / 故障切换候选
│   ├── stats.py           # 内存用量统计
│   ├── store.py           # SQLite 用量持久化
│   ├── requestlog.py      # 请求日志与重放（SQLite）
│   ├── cost.py            # 按 Key/模型成本记账（SQLite）
│   ├── metrics.py         # Prometheus 指标（无依赖）
│   ├── ratelimit.py       # 内存令牌桶限流
│   ├── redislimiter.py    # Redis 分布式限流（Lua 令牌桶）
│   ├── circuit.py         # 熔断器（closed/open/half_open）
│   ├── admin.py           # Web 管理面板（单文件 HTML）
│   ├── schemas.py         # 请求模型
│   └── errors.py          # 异常与重试策略
├── examples/
│   ├── client.py          # Python 客户端示例
│   └── curl.sh            # curl 示例
└── tests/
    ├── test_core.py       # 核心逻辑单测
    ├── test_ratelimit.py  # 内存限流单测
    ├── test_redislimiter.py # Redis 限流单测（自动跳过）
    ├── test_circuit.py    # 熔断单测
    ├── test_store.py      # 持久化单测
    ├── test_metrics.py    # 指标单测
    ├── test_cost.py       # 成本单测
    ├── test_requestlog.py # 请求日志单测
    └── test_e2e.py        # 端到端测试（Mock 上游）
```

---

## 测试

```bash
pip install pytest
python -m pytest -q
```

测试覆盖：配置环境变量展开、路由权重/故障切换候选、无 Key Provider 跳过、重试策略、内存/Redis 限流、
熔断器三态迁移、SQLite 持久化与按日汇总、Prometheus 指标、成本记账、请求日志与重放，
以及本地 Mock 上游下的完整非流式 + 流式 + Embeddings + 语音转写端到端流程。

---

## 路线图（Roadmap）

- [x] 持久化用量统计（SQLite）
- [x] 按 Key / Provider 限流（令牌桶）
- [x] 上游健康检查与自动熔断（circuit breaker）
- [x] 支持 `/v1/embeddings` 统一转发
- [x] 简单的 Web 管理面板
- [x] 请求日志与重放
- [x] 按 Key 记账与成本估算（结合各家 token 单价）
- [x] Prometheus 指标导出
- [x] `/v1/audio/transcriptions` 语音转写转发
- [x] 多进程/分布式限流（Redis 后端）

---

## License

[MIT](LICENSE)
