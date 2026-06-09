# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
from foretoken.bench.core.types import TurnResult
from foretoken.bench.report.metrics import (
    engine_requests_summary,
    engine_summary,
    goodput_ladder,
    percentiles,
    summarize,
    throughput,
)

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
    assert set(p) == {"ttft_ms", "tpot_ms", "e2e_ms", "gen_tok_s"}
    assert set(p["ttft_ms"]) == {"p50", "p90", "p99"}
    s = summarize(_results(), duration_s=10, gpu_bytes=2, num_gpus=2, slo=_SLO)
    assert s["completed"] == 2  # ok 轮
    assert s["total"] == 3
    assert len(s["goodput"]) == 3  # 三档 SLO
    assert s["throughput"]["output_tok_s"] == 15.0  # 原始吞吐(对照 goodput 严档 5.0)


def _spd(tpot, ok=True):
    return TurnResult(0, 0, ttft_ms=100, tpot_ms=tpot, e2e_ms=200, output_tokens=10, ok=ok)


def test_gen_tok_s_is_inverse_of_tpot():
    # 用户感知生成速度 = 1000/TPOT;高分位=更快,低分位 p10=慢尾(体感底线)
    p = percentiles([_spd(20), _spd(50)])["gen_tok_s"]
    assert set(p) == {"p10", "p50", "p90"}
    assert p["p90"] == 50.0  # 最快用户(TPOT 20ms)
    assert p["p10"] == 20.0  # 慢尾(TPOT 50ms)


def test_gen_tok_s_skips_zero_tpot():
    assert percentiles([_spd(0)])["gen_tok_s"]["p50"] == 0.0  # 单 token 轮无 TPOT


def test_engine_summary_aggregates_prefix_preempt_decode():
    stats = [
        {"kv": 0.5, "running": 2, "waiting": 1, "pc_q": 100,
         "pc_h": 40, "gen_tok": 10, "preempted": 1},
        {"kv": 0.8, "running": 3, "waiting": 5, "pc_q": 100,
         "pc_h": 60, "gen_tok": 20, "preempted": 2},
    ]
    e = engine_summary(stats, duration_s=10.0)
    assert e["peak_kv"] == 0.8 and e["max_waiting"] == 5 and e["max_running"] == 3
    assert e["prefix_hit_rate"] == 100 / 200  # (40+60)/(100+100) token 级
    assert e["total_preempted"] == 3
    assert e["decode_tok_s"] == 30 / 10.0  # (10+20)/dur


def test_engine_summary_prefix_none_when_no_queries():
    e = engine_summary([{"kv": 0.1, "running": 0, "waiting": 0}], duration_s=0.0)
    assert e["prefix_hit_rate"] is None  # pc_q=0 → 不除零
    assert e["decode_tok_s"] is None  # duration 0 → None


def test_engine_summary_empty_is_none():
    assert engine_summary(None) is None
    assert engine_summary([]) is None


def test_engine_requests_summary_breakdown():
    reqs = [
        {"finish_reason": "length", "queued_s": 0.1, "prefill_s": 0.05, "decode_s": 1.0,
         "mtpot_s": 0.02, "e2e_s": 1.2, "n_prompt": 100, "cached": 40},
        {"finish_reason": "abort", "queued_s": 0.3, "prefill_s": 0.05, "decode_s": 2.0,
         "mtpot_s": 0.04, "e2e_s": 2.4, "n_prompt": 200, "cached": 0},
    ]
    s = engine_requests_summary(reqs)
    assert s["count"] == 2
    assert s["finish_reason"] == {"length": 1, "abort": 1}  # 量化截断
    assert s["cached_tokens"] == 40 and s["prompt_tokens"] == 300
    assert s["cached_token_frac"] == 40 / 300
    assert "p50" in s["queued_ms"] and "p99" in s["queued_ms"]  # 秒→ms 分位


def test_engine_requests_summary_empty_is_none():
    assert engine_requests_summary(None) is None
    assert engine_requests_summary([]) is None
