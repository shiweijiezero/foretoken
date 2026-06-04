"""评测测试台(G1/G2,P0 重心)——只有真正 real 的评测才知道好坏(docs/04 / 07 / 10 / 13)。

**入口 / 主流程:`run.py`(`run_evaluation`)→ `python -m foretoken.bench`;读它就懂下面
六个工位怎么串成一条流水线。**

- goodput:每 GPU 字节秒 goodput(统一标尺)。
- oracle:离线最优上界(Belady / PFOO),作"弥合 LRU→最优差距"的分母。
- fidelity:门槛零(回放保真)——离线 hash_id 命中率 vs 实测 APC 命中率。
- workload:Mooncake trace 回放 + 缝合法骨架(依赖 vLLM 的部分见 TODO)。
- ablation:4 对照配置(全关 / 只 KV / 只 MTP / 全开)driver。
- metrics:KV / goodput 指标遥测(F1)。
"""
from foretoken.bench.fidelity import gate_zero_ok, offline_prefix_hit_rate
from foretoken.bench.goodput import (
    SLO,
    RequestRecord,
    attains_slo,
    goodput_per_gpu_byte_second,
    goodput_tokens_per_s,
    slo_attainment,
)
from foretoken.bench.oracle import belady_hit_rate

__all__ = [
    "SLO",
    "RequestRecord",
    "attains_slo",
    "goodput_per_gpu_byte_second",
    "goodput_tokens_per_s",
    "slo_attainment",
    "belady_hit_rate",
    "offline_prefix_hit_rate",
    "gate_zero_ok",
]
