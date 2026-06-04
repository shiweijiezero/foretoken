"""spec_decode —— MTP 投机控制(C2,P2/P3)。对齐 vLLM `v1/spec_decode/`。

C1(MTP 本体)= 复用 vLLM 原生 MTP(不自建 drafter);C2(自建)= goodput 感知的自适应投机长度。

接入(零 fork,docs/05 / 08):新算法走 custom_class proposer(vLLM
`v1/spec_decode/custom_class_proposer.py`;cross_vocab 走过这条);自适应 K 注入点 =
num_lookahead_tokens,受 cudagraph 桶约束。

可守利基(docs/11):goodput 感知 + 与 KV 命中 / 调度跨层耦合 + MTP-on-EP'd-MoE 专用
(主线 #26504 / #36657 两条 open 但均非 goodput 感知)。draft KV 必须标 EPHEMERAL(C1<->B3 契约)。
"""
