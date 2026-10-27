# 贡献指南 · Contributing

感谢你对 polygate 的关注！欢迎提交 Issue 与 Pull Request。

## 环境准备

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -e ".[dev]"
```

## 开发流程

1. Fork 本仓库并从 `main` 拉出功能分支。
2. 修改代码，尽量附带/更新单元测试。
3. 本地跑测试与静态检查：

   ```bash
   pytest
   ruff check .
   ```

4. 提交时遵循 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)（如 `fix:`、`feat:`、`docs:`）。
5. 发起 PR，描述改动动机与影响范围，关联相关 Issue。

## 测试说明

- 测试无需外部网络，内置 Mock 上游覆盖 chat（流式/非流式）、embeddings、语音转写、日志与重放、配置热重载等。
- Redis 相关测试在无 Redis 时自动跳过。
- 改动 `config.example.yaml` 的字段时，请同步更新 `README.md` 与 `config.yaml` 注释说明。

## 代码风格

- 使用 ruff（配置见 `pyproject.toml`）。
- 保持现有中文注释风格；公开 API 尽量附一句说明。

## 版本发布

- 版本号唯一来源是 `polygate/__init__.py` 的 `__version__`。
- 发布时更新 `CHANGELOG.md` 并打对应 git tag（如 `v0.2.0`）。
