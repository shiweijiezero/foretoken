"""每 GPU 字节秒 goodput —— Foretoken 的统一标尺。

定义(SLO 约束下的有效产出密度):

    goodput_per_gpu_byte_second =
        (满足 SLO 的请求贡献的 output token 数) / (wall_clock_seconds * gpu_bytes)

口径说明:
- "满足 SLO" = 该请求 TTFT <= slo.ttft_ms 且 TPOT <= slo.tpot_ms(联合,docs/04)。
- 分母含 gpu_bytes 是本项目差异点(每字节密度,docs/03 / 07);厂商常只报裸吞吐。
- TPOT 口径(mean / P99)由上层采集决定,这里只做阈值判定、不假设。
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class SLO:
    ttft_ms: float
    tpot_ms: float

    @classmethod
    def relative(
        cls, base_ttft_ms: float, base_tpot_ms: float, ttft_x: float, tpot_x: float
    ) -> "SLO":
        """相对倍数 SLO(docs/07:如 TTFT_P90 <= 10x、TPOT_P90 <= 5x)。"""
        return cls(ttft_ms=base_ttft_ms * ttft_x, tpot_ms=base_tpot_ms * tpot_x)


@dataclass
class RequestRecord:
    ttft_ms: float
    tpot_ms: float
    output_tokens: int


def attains_slo(r: RequestRecord, slo: SLO) -> bool:
    return r.ttft_ms <= slo.ttft_ms and r.tpot_ms <= slo.tpot_ms


def good_output_tokens(records: Iterable[RequestRecord], slo: SLO) -> int:
    return sum(r.output_tokens for r in records if attains_slo(r, slo))


def slo_attainment(records: Iterable[RequestRecord], slo: SLO) -> float:
    records = list(records)
    if not records:
        return 0.0
    return sum(attains_slo(r, slo) for r in records) / len(records)


def goodput_per_gpu_byte_second(
    records: Iterable[RequestRecord],
    slo: SLO,
    duration_s: float,
    gpu_bytes: float,
) -> float:
    if duration_s <= 0:
        raise ValueError("duration_s must be > 0")
    if gpu_bytes <= 0:
        raise ValueError("gpu_bytes must be > 0")
    return good_output_tokens(records, slo) / (duration_s * gpu_bytes)


def goodput_tokens_per_s(
    records: Iterable[RequestRecord], slo: SLO, duration_s: float
) -> float:
    """不除 gpu_bytes 的 goodput(token/s);同 GPU 预算下跨配置对比的便捷视图。"""
    if duration_s <= 0:
        raise ValueError("duration_s must be > 0")
    return good_output_tokens(records, slo) / duration_s
