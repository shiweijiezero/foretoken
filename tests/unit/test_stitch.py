from foretoken.bench.stitch import fill_sessions, reconstruct_sessions, to_turns


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
    sessions = reconstruct_sessions(rows)
    assert len(sessions) == 2
    # r0,r1 同会话;r2 单独(只共享开头不算延续)
    assert sorted(len(s) for s in sessions) == [1, 2]


def test_fill_sessions_accumulates_and_uses_timestamps():
    sessions = [[{"timestamp": 0}, {"timestamp": 5000}]]  # 1 会话,2 轮
    conversations = [
        {"conversations": [
            {"from": "human", "value": "h1"}, {"from": "gpt", "value": "g1"},
            {"from": "human", "value": "h2"}, {"from": "gpt", "value": "g2"},
        ]}
    ]
    out = list(fill_sessions(sessions, conversations, len_fn=len, seed=0))
    assert len(out) == 2
    assert out[1]["prompt"].startswith(out[0]["prompt"])  # ★ 会话内累积复用
    assert out[0]["timestamp_ms"] == 0
    assert out[1]["timestamp_ms"] == 5000
    assert out[0]["expected_output_len"] == len("g1")


def test_fill_sessions_skips_too_short_conversation():
    sessions = [[{"timestamp": 0}, {"timestamp": 1}, {"timestamp": 2}]]  # 需 3 轮
    conversations = [
        {"conversations": [{"from": "human", "value": "a"}, {"from": "gpt", "value": "b"}]},  # 1 轮,不够
    ]
    assert list(fill_sessions(sessions, conversations, len_fn=lambda s: 1)) == []
