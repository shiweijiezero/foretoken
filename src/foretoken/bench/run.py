"""bench 的入口 / 主流程地图 —— 一次完整评测怎么跑。

    python -m foretoken.bench   →   run_evaluation(...)

读这一个文件就懂 bench/ 六个工位怎么协作(细节见各模块):

    workload   →   fidelity   →   ablation × CONFIGS   →   goodput   →   oracle
    (缝合负载)     (门槛零验货)      (4 对照各跑一遍)        (打分)       (对比最优上界)
"""
from __future__ import annotations

from foretoken.bench import ablation, fidelity, goodput, oracle, workload


def run_evaluation(
    *,
    trace_path: str,
    prompt_pool: list[str],
    slo: goodput.SLO,
    gpu_bytes: float,
    capacity_blocks: int,
    measured_native_hit: float | None = None,
) -> dict[str, object]:
    """跑一次完整评测,返回 {config_name: 该配置结果} + 最优上界。

    现为**编排骨架**:数据流已串好;依赖 vLLM 的步骤(workload 回放、ablation.run_config)
    内部仍是 TODO(见各模块)。读它即可看懂整条评测流水线。
    """
    # 1. 构造负载:真实 trace 骨架(时序/复用)+ prompt pool 内容(缝合法,docs/07)
    trace = workload.load_mooncake_trace(trace_path)        # TODO(P0): 解析 Mooncake jsonl
    requests = workload.stitch(trace, prompt_pool)          # TODO(P0): 缝合骨架 + 血肉

    # 2. 门槛零(开跑前验货,docs/04):离线前缀命中率 ≈ 实测原生 APC,否则回放失真、结论不可信
    hash_id_seqs = [r.hash_ids for r in requests]
    offline_hit = fidelity.offline_prefix_hit_rate(hash_id_seqs, capacity_blocks)
    if measured_native_hit is not None and not fidelity.gate_zero_ok(offline_hit, measured_native_hit):
        raise RuntimeError(
            f"门槛零未过:离线命中率 {offline_hit:.3f} vs 实测 APC {measured_native_hit:.3f} 偏差过大,"
            "回放可能失真——先修回放,别信下面的结论。"
        )

    # 3. 4 对照配置(全关/只KV/只MTP/全开)各跑一遍 → 每个返回该配置的 records + goodput
    results: dict[str, object] = {}
    for cfg in ablation.CONFIGS:
        results[cfg.name] = ablation.run_config(            # TODO(P0/P1): 起/连 vLLM + 回放 + 采集
            cfg, trace_path=trace_path, prompt_pool=prompt_pool, slo=slo, gpu_bytes=gpu_bytes
        )

    # 4. 最优上界(分母):Belady 给"理论最高命中率",衡量我们离天花板多近(docs/04 弥合差距)
    block_accesses = [b for seq in hash_id_seqs for b in seq]
    results["_oracle_hit_upper_bound"] = oracle.belady_hit_rate(block_accesses, capacity_blocks)

    return results
