"""kv_offload:价值感知 KV 卸载生命周期(B3,P1,MVP)。对应 vLLM `v1/kv_offload/`。

准入 / 驱逐 / TTL,目标为每 GPU 字节秒 goodput。接入方式(零 fork,docs/02 / 08):自定义
OffloadingManager 与 OffloadingSpec(经 spec_module_path 加载),以及自定义 CachePolicy
(ABC,vLLM `v1/kv_offload/cpu/policies/base.py`)。规范价值:

    Value(b) = P(reuse|Δt,class) * recompute_cost * SLO_value / (size * E[residency|class])

(docs/03)。GPU 层保持 LRU(无插件接缝);长生命周期状态保存在卸载层。价值算法(P(reuse) 等)
放在同级模块 plugins/cache_policy/(docs/06)。

TODO(P1):对接 vLLM ABC(`v1/kv_offload/base.py`、`cpu/policies/base.py`),实现 prepare_store
(准入)与 CachePolicy.evict(驱逐)。draft KV 必须标记为 EPHEMERAL、不可卸载(C1<->B3 契约,
docs/05)。P1 起步采用 token-type + S3FIFO(零模型推理,docs/06)。
"""
