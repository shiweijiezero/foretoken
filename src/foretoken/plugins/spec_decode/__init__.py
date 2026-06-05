"""spec_decode:MTP 投机控制(C2,P2/P3)。对应 vLLM `v1/spec_decode/`。

C1(MTP 本体)复用 vLLM 原生 MTP(不自建 drafter);C2(自建)为 goodput 感知的自适应投机长度。

接入方式(零 fork,docs/05 / 08):新算法采用 custom_class proposer(vLLM
`v1/spec_decode/custom_class_proposer.py`;cross_vocab 即沿此路径);自适应 K 的注入点为
num_lookahead_tokens,受 cudagraph 桶约束。

差异化定位(docs/11):goodput 感知 + 与 KV 命中 / 调度跨层耦合 + 面向 MTP-on-EP'd-MoE(上游
#26504 / #36657 两条 open PR 均非 goodput 感知)。draft KV 必须标记为 EPHEMERAL(C1<->B3 契约)。
"""
