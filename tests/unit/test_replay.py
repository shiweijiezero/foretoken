import pytest

from foretoken.bench.replay import ReplayResult, goodput_per_gpu_byte_second, relative_delays_ms


def test_relative_delays_preserve_gaps_and_concurrency():
    # 绝对 timestamp → 相对起跑;保留真实间隔,且同刻请求 → 同延迟(并发保真)
    assert relative_delays_ms([1000, 1000, 1500, 3000]) == [0.0, 0.0, 500.0, 2000.0]


def test_relative_delays_empty():
    assert relative_delays_ms([]) == []


def test_goodput_counts_only_slo_met():
    results = [
        ReplayResult("a", ttft_ms=100, tpot_ms=20, output_tokens=50, ok=True),  # 达标 → 计 50
        ReplayResult("b", ttft_ms=9999, tpot_ms=20, output_tokens=99, ok=True),  # TTFT 超 → 不计
        ReplayResult("c", ttft_ms=100, tpot_ms=20, output_tokens=10, ok=False),  # 失败 → 不计
    ]
    g = goodput_per_gpu_byte_second(results, ttft_ms=2000, tpot_ms=80, duration_s=10, gpu_bytes=2)
    assert g == 50 / (10 * 2)


def test_goodput_invalid_denominators():
    for bad in (0, -1):
        with pytest.raises(ValueError):
            goodput_per_gpu_byte_second([], ttft_ms=1, tpot_ms=1, duration_s=bad, gpu_bytes=1)
        with pytest.raises(ValueError):
            goodput_per_gpu_byte_second([], ttft_ms=1, tpot_ms=1, duration_s=1, gpu_bytes=bad)
