# 贡献指南

Foretoken 是基于 vLLM 的工业级推理 + 优化(KV / MTP / goodput)+ 真实评测项目。背景见 [README](README.md) 与 [`docs/`](docs/)。

## 开发环境

```bash
pip install -e .          # 安装依赖(含 vLLM)
pre-commit install        # 启用提交前钩子(ruff + 私网信息防泄漏)
```

vLLM 需 Linux + NVIDIA GPU;真实推理 / 评测在 GPU 机器上跑。纯函数与指标层(`bench/core/{types,workload}`、`bench/report`、`data_prepare` 的核心逻辑)不依赖 GPU,可在任意机器单测。

## 测试

```bash
pytest -m "not slow"      # 快子集(无 GPU);pre-commit 在 pre-push 跑此集
pytest -m slow            # 起 GPU / 端到端(手动或 CI)
```

marker 见 `pyproject.toml [tool.pytest.ini_options]`:`slow`(起 GPU / 端到端)、`lossless`(开优化 == 原生 vLLM)、`determinism`、`eval`。

## 代码规范

- **风格 / lint**:ruff(line-length 100,规则 `E/F/I/UP/B`);`ruff format`。提交前由 pre-commit 自动执行。
- **类型**:全量类型注解 + `from __future__ import annotations`;`mypy` 配置见 `pyproject.toml`。
- **依赖**:统一单一 `dependencies` 列表,顶层 `import`,不分 extra、不做惰性导入。
- **license header**:每个 `.py` 顶部两行:
  ```python
  # SPDX-License-Identifier: Apache-2.0
  # SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
  ```
- **注释 / 文档**:中立、专业、面向陌生贡献者;只陈述当前结论,不写过程叙述 / 临时代号 / emoji / 比喻。

## 结构

- `bench/replay.py` — 评测命令行入口(参数解析 + 编排)。
- `bench/core/` — 回放核心:`types`、`workload`(加载 / 窗口 / 采样 / 调度 / goodput)、`vllm_engine`(进程内 AsyncLLM + 闭环回放 + 监控)。
- `bench/report/` — 指标聚合与出图(`metrics` / `plots` / `markdown` / `writer`)。
- `data_prepare/` — 评测负载缝合与数据集打包。
- `plugins/` — 优化插件(P1 起,经 vLLM 官方扩展点接入,零 fork)。

## 提交流程

1. 从最新 `main` 切分支。
2. 保持单次提交聚焦;commit message 用陈述句简述改动与原因。
3. 确保 `pytest -m "not slow"` 与 `ruff check` 通过(pre-commit 会拦截)。
4. 开 PR,关联 issue,说明动机与验证方式。

## 安全

请勿把内网路径 / 私有 IP / 凭据写入会提交的文件;`infra/` 已在 `.gitignore`,pre-commit 另设 `forbid-*` 钩子作二次拦截。
