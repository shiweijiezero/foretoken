# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
import asyncio

import pytest

from foretoken.bench.core.loop import replay
from foretoken.bench.core.types import TurnResult
from foretoken.bench.core.workload import (
    deadline_seconds,
    goodput_per_gpu_byte_second,
    group_sessions,
    next_send_ms,
    parse_window,
    sample_sessions,
)


class _FakeBackend:
    """模拟后端:gen_once 分步生成(可被取消);记录被取消的请求 id。"""

    def __init__(self, step_delay):
        self.step_delay = step_delay
        self.aborted: list[str] = []

    async def gen_once(self, messages, request_id):
        try:
            for _ in range(3):
                await asyncio.sleep(self.step_delay)
            return "xxx", 1.0, 0.5, 3.0, 3, 4, True  # text, ttft, tpot, e2e, n, n_prompt, ok
        except asyncio.CancelledError:
            self.aborted.append(request_id)
            raise


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


def _sessions(n, turns_each=1):
    return {
        i: [{"session_id": i, "turn": t, "timestamp_ms": t} for t in range(turns_each)]
        for i in range(n)
    }


def test_sample_sessions_none_returns_all():
    s = _sessions(10)
    assert sample_sessions(s) is s  # 不给采样口径 → 原样


def test_sample_sessions_fraction_keeps_whole_sessions():
    s = _sessions(10, turns_each=2)
    out = sample_sessions(s, fraction=0.3, seed=0)
    assert len(out) == 3  # round(10*0.3)
    assert all(len(out[k]) == 2 for k in out)  # 整会话保留(多轮不拆)


def test_sample_sessions_n_requests_reaches_target():
    s = _sessions(50, turns_each=3)  # 每会话 3 轮
    out = sample_sessions(s, n_requests=10, seed=0)
    total = sum(len(v) for v in out.values())
    assert total >= 10  # 累计达目标(整会话保留,末个可能略超)
    assert total < 10 + 3  # 不会超过一个会话的轮数
    assert all(len(v) == 3 for v in out.values())  # 不拆会话


def test_sample_sessions_reproducible():
    s = _sessions(20)
    assert set(sample_sessions(s, fraction=0.5, seed=7)) == set(
        sample_sessions(s, fraction=0.5, seed=7)
    )


def test_deadline_seconds():
    # 上限 = 窗口跨度 × sec_multiplier + 宽限秒;sec_multiplier 压缩窗口跨度
    assert deadline_seconds((0, 300_000), sec_multiplier=1.0, tail_grace_s=300) == 600.0
    assert deadline_seconds((0, 300_000), sec_multiplier=0.5, tail_grace_s=300) == 450.0
    assert deadline_seconds((600_000, 1_200_000), sec_multiplier=1.0, tail_grace_s=0) == 600.0
    # 无窗口或宽限<0 → 不设限
    assert deadline_seconds(None, sec_multiplier=1.0, tail_grace_s=300) is None
    assert deadline_seconds((0, 300_000), sec_multiplier=1.0, tail_grace_s=-1) is None


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
    be = _FakeBackend(step_delay=0.001)
    results, cancelled, _health = asyncio.run(replay(sessions, be, deadline_s=None))
    assert len(results) == 2
    assert all(r.ok and r.output_tokens == 3 for r in results)
    assert cancelled == 0
    assert be.aborted == []  # 无取消 → 无 abort


def test_replay_deadline_cancels_inflight():
    sessions = {1: [{"session_id": 1, "turn": 0, "timestamp_ms": 0, "user": "hi", "system": None}]}
    be = _FakeBackend(step_delay=0.5)  # 单轮 ~1.5s,远超 deadline
    results, cancelled, _health = asyncio.run(replay(sessions, be, deadline_s=0.05))
    assert results == []  # 到点在飞被取消,无完成轮
    assert cancelled == 1
    assert be.aborted == ["1-0"]  # 取消时记录该请求


def test_goodput_invalid_denominators():
    for bad in (0, -1):
        with pytest.raises(ValueError):
            goodput_per_gpu_byte_second([], ttft_ms=1, tpot_ms=1, duration_s=bad, gpu_bytes=1)
        with pytest.raises(ValueError):
            goodput_per_gpu_byte_second([], ttft_ms=1, tpot_ms=1, duration_s=1, gpu_bytes=bad)
