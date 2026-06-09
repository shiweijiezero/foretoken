# tests/ —— 测试脚手架

测试随功能落地逐步填实,当前多为占位(以 skip 表达意图,保留目录与 markers)。

## 目录约定
- `correctness/` —— 代码正确性:无损校验(KV / MTP 逐 token 等价)、确定性、draft-KV EPHEMERAL 约定。
- `unit/` —— 纯函数:负载构造、指标统计等(不需 GPU)。
- `integration/` —— 插件在真实 vLLM 上加载 / 跑通 / 不崩。

## pytest markers(注册见 pyproject.toml `[tool.pytest.ini_options]`)
- `slow` —— 需 GPU / 端到端(不进 pre-commit;手动或 CI 跑)。
- `lossless` —— 无损校验。
- `determinism` —— 确定性 / batch-invariance。
- `eval` —— 评测正确性 gate。

快子集(`-m "not slow"`)进 pre-push(见 [`../.pre-commit-config.yaml`](../.pre-commit-config.yaml));慢的端到端只 CI / 手动跑。
