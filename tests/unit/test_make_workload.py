from foretoken.data_prepare.make_workload import fill_sessions, reconstruct_sessions, to_turns


def test_to_turns_pairs_and_system():
    conv = [
        {"from": "system", "value": "you are helpful"},
        {"from": "human", "value": "h1"}, {"from": "gpt", "value": "g1"},
        {"from": "human", "value": "h2"}, {"from": "gpt", "value": "g2"},
    ]
    system, turns = to_turns(conv)
    assert system == "you are helpful"
    assert turns == [("h1", "g1"), ("h2", "g2")]


def test_to_turns_multimodal_skipped():
    conv = [{"from": "human", "value": [{"img": "x"}]}, {"from": "gpt", "value": "d"}]
    assert to_turns(conv) == (None, [])


def test_reconstruct_sessions_not_fooled_by_shared_prefix():
    rows = [
        {"timestamp": 0, "hash_ids": [1, 2]},
        {"timestamp": 5, "hash_ids": [1, 2, 3]},   # 续 r0(真前缀延续)
        {"timestamp": 3, "hash_ids": [1, 9]},      # 另一会话:共享 hash[0]=1 但分叉
    ]
    sessions = reconstruct_sessions(rows, min_shared_blocks=2)
    assert len(sessions) == 2
    # r0,r1 同会话;r2 单独(只共享开头不算延续)
    assert sorted(len(s) for s in sessions) == [1, 2]


def test_reconstruct_tolerates_partial_tail_block():
    # 512 块尾部不满,下一轮累积会填满该块、其 hash 改变;故比去尾满块前缀而非完整 hash
    rows = [
        {"timestamp": 0, "hash_ids": [1, 2, 3]},      # 第 1 轮,尾块 3 不满
        {"timestamp": 5, "hash_ids": [1, 2, 9, 4]},   # 第 2 轮:满块 [1,2] 不变,尾块 3 被填满→9,加新块 4
    ]
    sessions = reconstruct_sessions(rows, min_shared_blocks=2)
    # 去尾比前缀 → 正确识别为同一会话;若比完整 hash([1,2,3] 非 [1,2,9,4] 前缀)会漏成 2 个单轮
    assert len(sessions) == 1
    assert len(sessions[0]) == 2


def test_reconstruct_rejects_concurrent_same_timestamp():
    # 同时刻的两个请求共享长前缀,是并发(非多轮),timestamp 不严格递增 → 不并入同一会话
    rows = [
        {"timestamp": 0, "hash_ids": [1, 2, 3]},
        {"timestamp": 0, "hash_ids": [1, 2, 3, 4]},
    ]
    assert len(reconstruct_sessions(rows, min_shared_blocks=2)) == 2


def test_fill_sessions_accumulates_and_uses_timestamps():
    sessions = [[{"timestamp": 0}, {"timestamp": 5000}]]  # 1 会话,2 轮
    conversations = [
        {"conversations": [
            {"from": "human", "value": "h1"}, {"from": "gpt", "value": "g1"},
            {"from": "human", "value": "h2"}, {"from": "gpt", "value": "g2"},
        ]}
    ]
    out = list(fill_sessions(sessions, conversations, seed=0))
    assert len(out) == 2
    assert out[1]["prompt"].startswith(out[0]["prompt"])  # 会话内累积复用
    assert out[0]["timestamp_ms"] == 0
    assert out[1]["timestamp_ms"] == 5000
    assert set(out[0]) == {"timestamp_ms", "prompt"}  # 只产两字段,不预设输出长度


def test_fill_sessions_skips_too_short_conversation():
    sessions = [[{"timestamp": 0}, {"timestamp": 1}, {"timestamp": 2}]]  # 需 3 轮
    conversations = [
        {"conversations": [{"from": "human", "value": "a"}, {"from": "gpt", "value": "b"}]},  # 1 轮,不够
    ]
    assert list(fill_sessions(sessions, conversations)) == []


def test_fill_sessions_matches_by_turns_without_waste():
    # 短对话不被长会话跳过丢弃:M=2 会话用 2 轮对话、M=3 会话用 3 轮对话,两者都填上(无浪费)
    def conv(n):
        msgs = []
        for i in range(n):
            msgs += [{"from": "human", "value": f"h{i}"}, {"from": "gpt", "value": f"g{i}"}]
        return {"conversations": msgs}

    sessions = [
        [{"timestamp": 0}, {"timestamp": 1}, {"timestamp": 2}],  # M=3
        [{"timestamp": 10}, {"timestamp": 11}],                  # M=2
    ]
    out = list(fill_sessions(sessions, [conv(2), conv(3)]))
    assert len(out) == 5  # 对话充足:长会话优先下两个会话仍都填上
    assert {r["timestamp_ms"] for r in out} == {0, 1, 2, 10, 11}


def test_fill_sessions_prefers_long_session_when_scarce():
    # 对话稀缺(只有一条 3 轮):长会话 M=3 应保住,短会话 M=2 被 drop(长会话优先)
    sessions = [
        [{"timestamp": 0}, {"timestamp": 1}],                       # M=2(短,牺牲)
        [{"timestamp": 10}, {"timestamp": 11}, {"timestamp": 12}],  # M=3(长,保住)
    ]
    conv3 = {"conversations": [
        {"from": "human", "value": "h0"}, {"from": "gpt", "value": "g0"},
        {"from": "human", "value": "h1"}, {"from": "gpt", "value": "g1"},
        {"from": "human", "value": "h2"}, {"from": "gpt", "value": "g2"},
    ]}
    out = list(fill_sessions(sessions, [conv3]))
    assert len(out) == 3                                     # 填了 M=3 会话
    assert {r["timestamp_ms"] for r in out} == {10, 11, 12}  # 长会话保住,短会话 drop


def test_fill_sessions_truncate_keeps_count_with_flag():
    # 无足够长对话时 truncate=True 截断会话保数量,并给行打 truncated 标记
    sessions = [[{"timestamp": 0}, {"timestamp": 1}, {"timestamp": 2}]]  # M=3
    conv2 = {"conversations": [
        {"from": "human", "value": "h0"}, {"from": "gpt", "value": "g0"},
        {"from": "human", "value": "h1"}, {"from": "gpt", "value": "g1"},
    ]}  # 只 2 轮
    out = list(fill_sessions(sessions, [conv2], truncate=True))
    assert len(out) == 2                           # 截断为 2 轮
    assert all(r.get("truncated") for r in out)    # 行打 truncated 标记
