from foretoken.bench.replay import TurnResult
from foretoken.bench.report import goodput_ladder, percentiles, summarize, throughput

_SLO = [(2000, 80), (10000, 150), (60000, 200)]


def _results():
    return [
        TurnResult(0, 0, ttft_ms=100, tpot_ms=20, e2e_ms=200, output_tokens=50, ok=True),
        TurnResult(0, 1, ttft_ms=5000, tpot_ms=100, e2e_ms=9000, output_tokens=100, ok=True),
        TurnResult(1, 0, ttft_ms=100, tpot_ms=20, e2e_ms=200, output_tokens=10, ok=False),
    ]


def test_goodput_ladder_tiers():
    rows = goodput_ladder(_results(), duration_s=10, gpu_bytes=2, num_gpus=2, slo=_SLO)
    strict, mid, _loose = rows
    # 严档(2s,80ms):仅 r1 达成(r2 TTFT 5000>2000)
    assert strict["attain"] == 0.5  # ok 计数 2,达成 1
    assert strict["good_tok_s"] == 5.0  # 50 tok / 10s
    assert strict["good_tok_s_per_gpu"] == 2.5  # /2 GPU
    assert strict["tok_per_s_gpubyte"] == 2.5  # 50/(10*2)
    # 中档(10s,150ms):r1+r2 都达成
    assert mid["attain"] == 1.0
    assert mid["good_tok_s"] == 15.0  # 150 tok / 10s


def test_goodput_ladder_no_gpu_bytes():
    rows = goodput_ladder(_results(), duration_s=10, gpu_bytes=0, num_gpus=2, slo=_SLO)
    assert rows[0]["tok_per_s_gpubyte"] is None  # 无显存信息 → 归一化置空


def test_throughput_raw_not_slo_filtered():
    # 原始吞吐:ok 轮全部输出 tok(50+100=150)/ 10s,不按 SLO 过滤
    t = throughput(_results(), duration_s=10, num_gpus=2)
    assert t["output_tok_s"] == 15.0
    assert t["output_tok_s_per_gpu"] == 7.5
    assert t["request_s"] == 0.2  # 2 ok / 10s


def test_percentiles_and_summary():
    p = percentiles(_results())
    assert set(p) == {"ttft_ms", "tpot_ms"}
    assert set(p["ttft_ms"]) == {"p50", "p90", "p99"}
    s = summarize(_results(), duration_s=10, gpu_bytes=2, num_gpus=2, slo=_SLO)
    assert s["completed"] == 2  # ok 轮
    assert s["total"] == 3
    assert len(s["goodput"]) == 3  # 三档 SLO
    assert s["throughput"]["output_tok_s"] == 15.0  # 原始吞吐(对照 goodput 严档 5.0)
