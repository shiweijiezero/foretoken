"""优化插件:接入 vLLM 官方扩展点的树外插件(零 fork,docs/02 / 08)。

子模块沿用 vLLM 命名,便于与上游对照(本地参考源码在 foretoken/vllm/):
- kv_offload:价值感知 KV 卸载(B3,P1,MVP),对应 vLLM v1/kv_offload/。
- spec_decode:MTP 投机控制(C2,P2),对应 vLLM v1/spec_decode/。
- cache_policy / sched / kv_connector:P1 起续建(见 docs/00 命名约定)。
"""
