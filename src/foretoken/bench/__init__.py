"""评测台(bench)。

**P0 评测直接用 `vllm bench serve`(见 scripts/serve_glm.sh + run_baseline.sh)——不自建
负载 / 采集 / 指标。** vLLM 已提供:流量分布(`--request-rate` / `--burstiness`,泊松↔Gamma)、
时序回放(`--dataset-name timed_trace`)、采集 TTFT/TPOT、按请求 goodput(`--goodput`)。

本模块留到 **P1**:评我们自己的 KV 策略时,才补 vLLM 没有的那几样——每 GPU 字节秒 goodput、
门槛零(回放保真)、PFOO/Belady 最优上界、4 配置拆贡献。**前期不实现**(这些是 P1 才需要的尺子)。

**已实现:`stitch.py`**(Mooncake-hash 缝真实文本块 → 带 timestamp 的 trace)+ **`replay.py`**
(按真实 timestamp 回放、采 TTFT/TPOT、算 goodput)= 含多用户并发的"一套全真"(KV+MTP 通吃)。
"""
from __future__ import annotations


def run_evaluation(*args, **kwargs):
    """评测我们自己优化(KV 策略等)的入口 —— P1 再实现。

    P0 不走这里:P0 评测 = `vllm bench serve`(scripts/run_baseline.sh)。
    """
    raise NotImplementedError("P0 用 vllm bench serve;本入口留到 P1(补 vLLM 没有的独有指标)。")
