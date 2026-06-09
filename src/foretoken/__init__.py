# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""Foretoken — 基于 vLLM 的工业级推理 + 优化(KV/MTP/goodput)+ 真实评测。

三支柱(见 README.md):
- 主体:工业级真实推理(复用 vLLM)。
- 优化:在 `plugins/` 下以树外插件实现(kv_offload / spec_decode),子模块沿用 vLLM 命名。
- 裁判:真实评测(data_prepare/ 生成负载,bench/ 回放评测)——以真实评测判断优化优劣。

零 fork:全程采用 vLLM 官方扩展点(spec_module_path / scheduler_cls / custom_class /
entry_points)。
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("foretoken")
except PackageNotFoundError:  # 源码树未安装(如 PYTHONPATH=src 直接运行)
    __version__ = "0.0.0"
