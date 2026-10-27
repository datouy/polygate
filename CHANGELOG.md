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

### 变更 Changed
- `/v1/models` 只返回 chat 模型，避免 embeddings / audio 混入 OpenAI SDK 模型列表。
- 上游响应头透传（`ratelimit-*`、`retry-after`、上游 `x-request-id`），并在 `X-Request-ID` 返回网关 `req_id`。
- 流式中途出错输出 SSE `error` 事件；上游 200 非 JSON 转为 502。
- `stream_options.include_usage` 自动注入改为可配置（`stream_include_usage`），失败自动去掉重试。
- 管理面板全部动态字段转义，修复存储型 XSS。
- 新增 `/readyz` 就绪检查；`redis` 拆为可选依赖（`requirements-redis.txt` / `pip install "polygate[redis]"`）。
- 新增 `pyproject.toml`（版本单一来源、ruff/mypy/pytest 配置）。

### 新增 Added
- CI（GitHub Actions：pytest + ruff + Docker 构建）。
- `Dockerfile` 与 `docker-compose.yml`。
- `CHANGELOG.md`、`CONTRIBUTING.md`、Issue/PR 模板。

## [0.1.0] - 初始版本
