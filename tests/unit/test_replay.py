import pytest

from foretoken.bench.replay import (
    TurnResult,
    goodput_per_gpu_byte_second,
    group_sessions,
    next_send_ms,
    parse_window,
)


def test_next_send_first_turn_absolute():
    # 首轮:按绝对 timestamp(相对 t0)
    assert next_send_ms(0, t_cur=1000, t_prev=1000, complete_prev_ms=0, t0=500) == 500.0


def test_next_send_on_time_uses_absolute():
    # 上一轮按时完成(C ≤ T_k 相对)→ 按绝对时间戳。t_cur_rel=2500,C=1500≤2500 → 2500
    assert next_send_ms(1, t_cur=3000, t_prev=1000, complete_prev_ms=1500, t0=500) == 2500.0


def test_next_send_overtime_completion_plus_gap():
    # 超时(C > T_k 相对)→ 完成时刻 + 原间隔(T_k−T_{k-1})。C=4000>2500 → 4000+(3000-1000)=6000
    assert next_send_ms(1, t_cur=3000, t_prev=1000, complete_prev_ms=4000, t0=500) == 6000.0


def test_group_sessions_sorts_by_turn():
    rows = [
        {"session_id": 1, "turn": 1, "timestamp_ms": 50},
        {"session_id": 1, "turn": 0, "timestamp_ms": 10},
        {"session_id": 2, "turn": 0, "timestamp_ms": 20},
    ]
    sess = group_sessions(rows)
    assert set(sess) == {1, 2}
    assert [r["turn"] for r in sess[1]] == [0, 1]  # 组内按 turn 排序


def test_group_sessions_window_admits_by_first_turn():
    # 准入按会话首轮 timestamp、整组纳入(后续轮拖到窗外也保留)
    rows = [
        {"session_id": 1, "turn": 0, "timestamp_ms": 0},  # 首轮在窗内
        {"session_id": 1, "turn": 1, "timestamp_ms": 999_999},  # 拖出窗也纳入
        {"session_id": 2, "turn": 0, "timestamp_ms": 600_000},  # 首轮在窗外 → 排除
    ]
    sess = group_sessions(rows, window=(0, 60_000))  # 前 1 分钟
    assert set(sess) == {1}
    assert len(sess[1]) == 2  # 整组纳入,不硬截


def test_parse_window():
    assert parse_window(None) is None
    assert parse_window("10") == (0, 600_000)
    assert parse_window("10:20") == (600_000, 1_200_000)


def test_goodput_counts_only_slo_met():
    # 达标计入(50);TTFT 超标不计;失败不计
    results = [
        TurnResult(0, 0, ttft_ms=100, tpot_ms=20, e2e_ms=200, output_tokens=50, ok=True),
        TurnResult(0, 1, ttft_ms=9999, tpot_ms=20, e2e_ms=999, output_tokens=99, ok=True),
        TurnResult(1, 0, ttft_ms=100, tpot_ms=20, e2e_ms=200, output_tokens=10, ok=False),
    ]
    g = goodput_per_gpu_byte_second(results, ttft_ms=2000, tpot_ms=80, duration_s=10, gpu_bytes=2)
    assert g == 50 / (10 * 2)


def test_goodput_invalid_denominators():
    for bad in (0, -1):
        with pytest.raises(ValueError):
            goodput_per_gpu_byte_second([], ttft_ms=1, tpot_ms=1, duration_s=bad, gpu_bytes=1)
        with pytest.raises(ValueError):
            goodput_per_gpu_byte_second([], ttft_ms=1, tpot_ms=1, duration_s=1, gpu_bytes=bad)
