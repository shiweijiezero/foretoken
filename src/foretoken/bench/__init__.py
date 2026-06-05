"""评测台(bench):回放负载、采集指标、计算 goodput。

P0 评测直接用 `vllm bench serve`(见 scripts/serve_glm.sh、run_baseline.sh),不自建负载 / 采集 /
指标:vLLM 已提供流量分布(`--request-rate` / `--burstiness`,泊松↔Gamma)、时序回放
(`--dataset-name timed_trace`)、采集 TTFT/TPOT、按请求 goodput(`--goodput`)。

已实现 `replay.py`:按真实 timestamp 异步回放 data_prepare/ 缝合的负载,采集 TTFT/TPOT、计算每 GPU
字节秒 goodput。自建回放的原因——vLLM `timed_trace` 仅接受 hash、`custom` 不含 timestamp,二者均
无法同时承载真实 prompt 与真实到达时刻(负载的缝合 / 打包在 data_prepare/)。

留到 P1:评测自有 KV 策略时,补足 vLLM 未提供的指标——门槛零(回放保真)、PFOO/Belady 最优上界、
4 配置拆贡献;run_evaluation 是该入口的占位。
"""
from __future__ import annotations


def run_evaluation(*args, **kwargs):
    """评测自有优化(KV 策略等)的入口,P1 再实现。

    P0 不使用此入口:P0 评测用 `vllm bench serve`(scripts/run_baseline.sh)。
    """
    raise NotImplementedError("P0 使用 vllm bench serve;此入口留到 P1(补足 vLLM 未提供的指标)。")
