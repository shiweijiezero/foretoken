from foretoken.bench.goodput import (
    SLO,
    RequestRecord,
    attains_slo,
    goodput_per_gpu_byte_second,
    goodput_tokens_per_s,
    slo_attainment,
)


def test_attains_slo_boundary_inclusive():
    slo = SLO(ttft_ms=2000, tpot_ms=80)
    assert attains_slo(RequestRecord(2000, 80, 100), slo)       # 边界含等号
    assert not attains_slo(RequestRecord(2001, 80, 100), slo)   # TTFT 超
    assert not attains_slo(RequestRecord(2000, 81, 100), slo)   # TPOT 超


def test_goodput_counts_only_slo_attaining_tokens():
    slo = SLO(ttft_ms=2000, tpot_ms=80)
    records = [
        RequestRecord(1000, 50, 100),   # 满足 → 计 100
        RequestRecord(5000, 50, 999),   # TTFT 超 → 不计
        RequestRecord(1000, 200, 50),   # TPOT 超 → 不计
    ]
    assert goodput_per_gpu_byte_second(records, slo, duration_s=10.0, gpu_bytes=2.0) == 100 / 20.0
    assert goodput_tokens_per_s(records, slo, duration_s=10.0) == 10.0


def test_slo_attainment_fraction():
    slo = SLO(ttft_ms=2000, tpot_ms=80)
    records = [RequestRecord(1000, 50, 10), RequestRecord(5000, 50, 10)]
    assert slo_attainment(records, slo) == 0.5


def test_slo_attainment_empty_is_zero():
    assert slo_attainment([], SLO(2000, 80)) == 0.0


def test_relative_slo():
    slo = SLO.relative(base_ttft_ms=200, base_tpot_ms=20, ttft_x=10, tpot_x=5)
    assert slo.ttft_ms == 2000
    assert slo.tpot_ms == 100


def test_invalid_denominators_raise():
    slo = SLO(2000, 80)
    rec = [RequestRecord(1, 1, 1)]
    for bad in (0.0, -1.0):
        try:
            goodput_per_gpu_byte_second(rec, slo, duration_s=bad, gpu_bytes=1.0)
            assert False, "expected ValueError"
        except ValueError:
            pass
        try:
            goodput_per_gpu_byte_second(rec, slo, duration_s=1.0, gpu_bytes=bad)
            assert False, "expected ValueError"
        except ValueError:
            pass
