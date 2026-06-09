# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""优化插件:接入 vLLM 官方扩展点的树外插件(零 fork)。

子模块沿用 vLLM 命名,便于与上游源码对照:
- kv_offload:价值感知 KV 卸载,对应 vLLM `v1/kv_offload/`。
- spec_decode:MTP 投机控制,对应 vLLM `v1/spec_decode/`。
"""
