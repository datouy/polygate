# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式；版本号遵循语义化版本。
版本号唯一来源是 `polygate/__init__.py` 里的 `__version__`（发布时同步打 git tag）。

## [Unreleased]

### 修复 Fixed
- 请求日志超长响应不再做字符切片，改为结构裁剪/整体丢弃 + `truncated` 标记，修复详情/重放接口 500（`JSONDecodeError`）。
- 请求日志新增 `upstream_model` 字段；重放使用对外模型名 + 独立 payload 副本，修复模型别名重放 404。
- 熔断器新增 `record_probe()`，健康探测不再清零业务失败计数。
- 多 Key 轮询在 401/403/429 时切换到下一个 Key（此前直接失败退出）。
- 流式请求在流结束后解析 SSE `usage` 并记账（token 与成本不再恒为 0）。
- 每个 Provider 的 `timeout` 配置真正生效。
- SQLite 写改为单连接 + 后台批量写入，消除事件循环阻塞与多连接写锁竞争。
- 热重载保留既有熔断器与限流器实例，不再重置失败计数、不再泄漏 Redis 连接池。
- 健康检查遍历全部 Key；401/403/404/5xx 不再被当作健康。
- `create_app` 状态挂载到实例局部作用域，去除模块级全局单例与 import 副作用。
- 删除 `config.py` 中会泄露明文密钥的死代码 `save_config`。
- 缺失环境变量启动时告警（不再静默降级为空串）。
- 管理台路由权重输入改为 `step=0.01`，与后端允许的正浮点权重一致；修复整数权重被浏览器误判无效。

### 变更 Changed
- 管理台拆分为总览、路由、用量与成本、请求、诊断、设置六个工作区，改为可直接打包的原生 HTML/CSS/JS；增加 hash 路由、请求详情、分页、可操作待处理项与机器可读页面状态。
- 管理台指标明确标注“本进程”和“近 30 天”口径；`/v1/usage` 返回 `scope: process`，`/v1/costs` 返回 `scope: rolling` 与 `window_days: 30`。
- `/v1/models` 只返回 chat 模型，避免 embeddings / audio 混入 OpenAI SDK 模型列表。
- 上游响应头透传（`ratelimit-*`、`retry-after`、上游 `x-request-id`），并在 `X-Request-ID` 返回网关 `req_id`。
- 流式中途出错输出 SSE `error` 事件；上游 200 非 JSON 转为 502。
- `stream_options.include_usage` 自动注入改为可配置（`stream_include_usage`），失败自动去掉重试。
- 管理面板全部动态字段转义，修复存储型 XSS。
- 新增 `/readyz` 就绪检查；`redis` 拆为可选依赖（`requirements-redis.txt` / `pip install "polygate[redis]"`）。
- 新增 `pyproject.toml`（版本单一来源、ruff/mypy/pytest 配置）。
- 配置写入改为 revision / ETag 校验并通过 `POST /v1/config/validate` 预审 diff，避免并发覆盖和无效配置上线。
- 请求页改为按逻辑请求展示；原请求详情接口继续保留，历史无 `logical_id` 的记录仍按单次请求展示。
- 兼容性结果可在管理台确认 diff 后合并到路由能力；`/models` 仍只作为端点诊断。

### 新增 Added
- 请求重放实验室：按历史 `req_id` 将同一请求依次重放到多个公开模型，对比 Provider、上游模型、延迟、成本与响应，并限制单次最多 20 个目标。
- Provider 兼容性矩阵：持久化探测基础对话、流式 usage、工具调用、JSON Mode 与 `/models`，提供管理接口、面板和最多 200 次探针调用保护。
- 硬预算账本：使用 SQLite `BEGIN IMMEDIATE` 原子预占月预算租约，按 usage 结算；缺少 usage 时按预占上限保守结算，面板展示 settled/reserved 状态。
- 能力约束式路由：支持模型级/路由级 `capabilities`、自动能力推导、`X-Polygate-Require` 与 `polygate_requirements` 显式约束。
- CI（GitHub Actions：pytest + ruff + Docker 构建）。
- `Dockerfile` 与 `docker-compose.yml`。
- `CHANGELOG.md`、`CONTRIBUTING.md`、Issue/PR 模板。
- 虚拟 Key 生命周期：创建、编辑、轮换和软吊销；数据库只存 SHA-256 哈希与短前缀，明文仅在创建/轮换时返回一次。
- Provider 保存前测试：可提交尚未保存的地址和 Key，返回可达性、状态码、延迟与模型列表，不写配置。
- Provider 密钥加密存储：覆盖库只保存 Fernet 密文与版本化引用；支持 `POLYGATE_MASTER_KEY`，未配置时生成受忽略的本地主密钥文件，并自动迁移旧明文覆盖。
- 逻辑请求分组接口与 attempt 链详情，展示 Provider 切换顺序、每次状态码、延迟和最终响应。

## [0.1.0] - 初始版本
