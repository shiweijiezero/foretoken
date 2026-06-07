import asyncio
from types import SimpleNamespace

import pytest

from foretoken.bench.replay import (
    TurnResult,
    deadline_seconds,
    goodput_per_gpu_byte_second,
    group_sessions,
    next_send_ms,
    parse_window,
    replay,
)


class _FakeTok:
    def apply_chat_template(self, messages, add_generation_prompt, tokenize):
        return "P"


class _FakeEngine:
    """模拟流式引擎:每轮分步产出累积 RequestOutput;记录被 abort 的请求。"""

    def __init__(self, step_delay):
        self.step_delay = step_delay
        self.aborted: list[str] = []

    async def generate(self, prompt, sampling_params, request_id):
        text = ""
        for i in range(3):
            await asyncio.sleep(self.step_delay)
            text += "x"
            yield SimpleNamespace(outputs=[SimpleNamespace(token_ids=list(range(i + 1)), text=text)])

    async def abort(self, request_id):
        self.aborted.append(request_id)


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


def test_group_sessions_window_truncates_out_of_window_turns():
    # 首轮在窗内才纳入;后续轮真实 ts 超窗 → 截断(只留窗内轮)
    rows = [
        {"session_id": 1, "turn": 0, "timestamp_ms": 0},  # 首轮在窗内
        {"session_id": 1, "turn": 1, "timestamp_ms": 30_000},  # 窗内(30s ≤ 60s)→ 留
        {"session_id": 1, "turn": 2, "timestamp_ms": 999_999},  # 真实 ts 超窗 → 截断
        {"session_id": 2, "turn": 0, "timestamp_ms": 600_000},  # 首轮超窗 → 整会话排除
    ]
    sess = group_sessions(rows, window=(0, 60_000))  # 前 1 分钟
    assert set(sess) == {1}
    assert [r["turn"] for r in sess[1]] == [0, 1]  # 截断到窗内轮(turn 2 真实 ts 超窗去掉)


def test_parse_window():
    assert parse_window(None) is None
    assert parse_window("10") == (0, 600_000)
    assert parse_window("10:20") == (600_000, 1_200_000)


def test_deadline_seconds():
    # 窗口跨度 5 分钟 × factor 2 = 600s;sec_multiplier 压缩同比例缩短
    assert deadline_seconds((0, 300_000), sec_multiplier=1.0, tail_factor=2.0) == 600.0
    assert deadline_seconds((0, 300_000), sec_multiplier=0.5, tail_factor=2.0) == 300.0
    assert deadline_seconds((600_000, 1_200_000), sec_multiplier=1.0, tail_factor=2.0) == 1200.0
    # 无窗口或 factor<=0 → 不设限
    assert deadline_seconds(None, sec_multiplier=1.0, tail_factor=2.0) is None
    assert deadline_seconds((0, 300_000), sec_multiplier=1.0, tail_factor=0) is None


def test_goodput_counts_only_slo_met():
    # 达标计入(50);TTFT 超标不计;失败不计
    results = [
        TurnResult(0, 0, ttft_ms=100, tpot_ms=20, e2e_ms=200, output_tokens=50, ok=True),
        TurnResult(0, 1, ttft_ms=9999, tpot_ms=20, e2e_ms=999, output_tokens=99, ok=True),
        TurnResult(1, 0, ttft_ms=100, tpot_ms=20, e2e_ms=200, output_tokens=10, ok=False),
    ]
    g = goodput_per_gpu_byte_second(results, ttft_ms=2000, tpot_ms=80, duration_s=10, gpu_bytes=2)
    assert g == 50 / (10 * 2)


def test_replay_completes_without_deadline():
    sessions = {
        1: [{"session_id": 1, "turn": 0, "timestamp_ms": 0, "user": "hi", "system": None}],
        2: [{"session_id": 2, "turn": 0, "timestamp_ms": 0, "user": "yo", "system": None}],
    }
    eng = _FakeEngine(step_delay=0.001)
    results = asyncio.run(replay(sessions, eng, _FakeTok(), sampling_params=None, deadline_s=None))
    assert len(results) == 2
    assert all(r.ok and r.output_tokens == 3 for r in results)
    assert eng.aborted == []  # 无取消 → 无 abort


def test_replay_deadline_cancels_inflight():
    sessions = {1: [{"session_id": 1, "turn": 0, "timestamp_ms": 0, "user": "hi", "system": None}]}
    eng = _FakeEngine(step_delay=0.5)  # 单轮 ~1.5s,远超 deadline
    results = asyncio.run(replay(sessions, eng, _FakeTok(), sampling_params=None, deadline_s=0.05))
    assert results == []  # 到点在飞被取消,无完成轮
    assert eng.aborted == ["1-0"]  # 取消时 abort 了该请求


def test_goodput_invalid_denominators():
    for bad in (0, -1):
        with pytest.raises(ValueError):
            goodput_per_gpu_byte_second([], ttft_ms=1, tpot_ms=1, duration_s=bad, gpu_bytes=1)
        with pytest.raises(ValueError):
            goodput_per_gpu_byte_second([], ttft_ms=1, tpot_ms=1, duration_s=1, gpu_bytes=bad)
