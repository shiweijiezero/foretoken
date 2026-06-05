"""评测台(bench):回放负载、采集指标、算 goodput。

P0 评测直接用 `vllm bench serve`(见 scripts/serve_glm.sh + run_baseline.sh),不自建负载 /
采集 / 指标:vLLM 已提供流量分布(`--request-rate` / `--burstiness`,泊松↔Gamma)、时序回放
(`--dataset-name timed_trace`)、采集 TTFT/TPOT、按请求 goodput(`--goodput`)。

已实现 `replay.py`:按真实 timestamp 异步回放 data_prepare/ 缝合的负载,采 TTFT/TPOT、算每 GPU
字节秒 goodput。回放自建的原因——vLLM `timed_trace` 仅认 hash、`custom` 无 timestamp,二者均吃
不下"真实 prompt + 真实到达"(负载的缝合 / 打包在姊妹包 data_prepare/)。

留到 P1:评自己的 KV 策略时,补 vLLM 没有的那几样——门槛零(回放保真)、PFOO/Belady 最优上界、
4 配置拆贡献;run_evaluation 是该入口的占位。
"""
from __future__ import annotations


def run_evaluation(*args, **kwargs):
    """评测我们自己优化(KV 策略等)的入口 —— P1 再实现。

    P0 不走这里:P0 评测 = `vllm bench serve`(scripts/run_baseline.sh)。
    """
    raise NotImplementedError("P0 用 vllm bench serve;本入口留到 P1(补 vLLM 没有的独有指标)。")
