# tests/ —— 测试脚手架(占位)

> **P0 前为空。** 项目尚无代码 → 此目录先立结构与约定;P0 起代码后逐步填。
> 测试纲领与判据见 [`../docs/13-testing-and-correctness.md`](../docs/13-testing-and-correctness.md)。

## 目录约定
- `correctness/` —— **一等测试**:无损校验(KV / MTP 逐 token 等价)、确定性、draft-KV EPHEMERAL 契约。
- `unit/` —— 纯函数:价值函数、`P(reuse)` 估计器、复用 vs 重算成本模型(可不起 GPU)。
- `integration/` —— 插件在真实 vLLM 上加载 / 跑通 / 不崩。
- `eval/` —— 评测正确性 gate:门槛零(回放保真)、PFOO / Belady oracle 作为可执行断言。

## pytest markers(P0 起在 pyproject / pytest.ini 注册)
- `slow` —— 起 GPU / 端到端(**不进** pre-commit;手动或 CI 跑)。
- `lossless` —— 无损校验。
- `determinism` —— 确定性 / batch-invariance。
- `eval` —— 评测 gate。

快子集(unit + 快 correctness)进 `pre-push`(见 [`../.pre-commit-config.yaml`](../.pre-commit-config.yaml)
占位);慢的端到端只 CI / 手动。
