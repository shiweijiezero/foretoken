"""Foretoken — 基于 vLLM 的工业级推理 + 优化(KV/MTP/goodput)+ 真实评测。

三支柱(见 README.md / docs/):
- 主体:工业级真实推理(复用 vLLM)。
- 优化:P1 起在 `plugins/` 下,子模块**对齐 vLLM 命名**(kv_offload / cache_policy / sched /
  spec_decode / kv_connector);现尚未实现(P0 不需要,不过早建空占位)。
- 裁判(bench/):真实评测——只有真正 real 的评测才知道好坏。

零 fork:全程走 vLLM 官方扩展点(spec_module_path / scheduler_cls / custom_class /
entry_points,见 docs/08)。
"""

__version__ = "0.0.0"
