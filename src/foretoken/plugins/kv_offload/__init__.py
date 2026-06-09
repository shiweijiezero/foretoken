# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""kv_offload:价值感知 KV 卸载(准入 / 驱逐 / TTL)。对应 vLLM `v1/kv_offload/`。

目标为每 GPU 字节秒 goodput。接入方式(零 fork):自定义 OffloadingManager 与 OffloadingSpec
(经 spec_module_path 加载),以及自定义 CachePolicy(ABC,vLLM
`v1/kv_offload/cpu/policies/base.py`)。GPU 层保持 LRU(无插件接缝),长生命周期状态保存在卸载层。

draft KV 标记为 EPHEMERAL、不可卸载(与 spec_decode 的约定)。
"""
