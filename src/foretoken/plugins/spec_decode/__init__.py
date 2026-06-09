# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""spec_decode:MTP 投机控制。对应 vLLM `v1/spec_decode/`。

MTP 本体复用 vLLM 原生 MTP(不自建 drafter);自建部分为 goodput 感知的自适应投机长度。

接入方式(零 fork):新算法采用 custom_class proposer(vLLM
`v1/spec_decode/custom_class_proposer.py`);自适应投机长度的注入点为 num_lookahead_tokens,
受 cudagraph 桶约束。draft KV 标记为 EPHEMERAL(与 kv_offload 的约定)。
"""
