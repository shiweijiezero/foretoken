# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""Foretoken — 基于 vLLM 的工业级推理 + 优化(KV/MTP/goodput)+ 真实评测。

三支柱(见 README.md / docs/):
- 主体:工业级真实推理(复用 vLLM)。
- 优化:P1 起在 `plugins/` 下,子模块沿用 vLLM 命名(kv_offload / cache_policy / sched /
  spec_decode / kv_connector);P0 不需要,暂不预建空占位。
- 裁判:真实评测(data_prepare/ 生成负载,bench/ 回放评测)——以真实评测判断优化优劣。

零 fork:全程采用 vLLM 官方扩展点(spec_module_path / scheduler_cls / custom_class /
entry_points,见 docs/08)。
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("foretoken")
except PackageNotFoundError:  # 源码树未安装(如 PYTHONPATH=src 直接运行)
    __version__ = "0.0.0"
