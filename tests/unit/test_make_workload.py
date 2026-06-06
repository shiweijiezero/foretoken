from foretoken.data_prepare.make_workload import (
    fill_sessions,
    group_by_session,
    reconstruct_sessions,
    to_turns,
)


def test_to_turns_pairs_and_system():
    conv = [
        {"from": "system", "value": "you are helpful"},
        {"from": "human", "value": "h1"},
        {"from": "gpt", "value": "g1"},
        {"from": "human", "value": "h2"},
        {"from": "gpt", "value": "g2"},
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
        {"timestamp": 5, "hash_ids": [1, 2, 3]},  # 续 r0(真前缀延续)
        {"timestamp": 3, "hash_ids": [1, 9]},  # 另一会话:共享 hash[0]=1 但分叉
    ]
    sessions = reconstruct_sessions(rows, min_shared_blocks=2)
    assert len(sessions) == 2
    # r0,r1 同会话;r2 单独(只共享开头不算延续)
    assert sorted(len(s) for s in sessions) == [1, 2]


def test_reconstruct_tolerates_partial_tail_block():
    # 512 块尾部不满,下一轮累积会填满该块、其 hash 改变;故比去尾满块前缀而非完整 hash
    rows = [
        {"timestamp": 0, "hash_ids": [1, 2, 3]},  # 第 1 轮,尾块 3 不满(partial)
        {"timestamp": 5, "hash_ids": [1, 2, 9, 4]},  # 第 2 轮:满块 [1,2] 不变,尾块填满→9
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
        {
            "conversations": [
                {"from": "human", "value": "h1"},
                {"from": "gpt", "value": "g1"},
                {"from": "human", "value": "h2"},
                {"from": "gpt", "value": "g2"},
            ]
        }
    ]
    out = list(fill_sessions(sessions, conversations, seed=0))
    assert len(out) == 2
    assert out[1]["prompt"].startswith(out[0]["prompt"])  # 会话内累积复用
    assert out[0]["timestamp_ms"] == 0
    assert out[1]["timestamp_ms"] == 5000
    assert set(out[0]) == {"timestamp_ms", "prompt"}  # 只产两字段,不预设输出长度


def test_fill_sessions_empty_pool_yields_nothing():
    # 对话池为空(全为多模态、被 to_turns 跳过)→ 无可填,输出空
    sessions = [[{"timestamp": 0}, {"timestamp": 1}]]
    multimodal = [{"conversations": [{"from": "human", "value": [{"img": "x"}]}]}]
    assert list(fill_sessions(sessions, multimodal)) == []


def test_fill_sessions_matches_by_turns_without_waste():
    # 短对话不被长会话跳过丢弃:M=2 会话用 2 轮对话、M=3 会话用 3 轮对话,两者都填上(无浪费)
    def conv(n):
        msgs = []
        for i in range(n):
            msgs += [{"from": "human", "value": f"h{i}"}, {"from": "gpt", "value": f"g{i}"}]
        return {"conversations": msgs}

    sessions = [
        [{"timestamp": 0}, {"timestamp": 1}, {"timestamp": 2}],  # M=3
        [{"timestamp": 10}, {"timestamp": 11}],  # M=2
    ]
    out = list(fill_sessions(sessions, [conv(2), conv(3)]))
    assert len(out) == 5  # 对话充足:长会话优先下两个会话仍都填上
    assert {r["timestamp_ms"] for r in out} == {0, 1, 2, 10, 11}


def test_fill_sessions_prefers_long_session_when_scarce():
    # 对话稀缺(只有一条 3 轮):长会话 M=3 优先拿到,短会话 M=2 因对话耗尽未填
    sessions = [
        [{"timestamp": 0}, {"timestamp": 1}],  # M=2(短)
        [{"timestamp": 10}, {"timestamp": 11}, {"timestamp": 12}],  # M=3(长,优先)
    ]
    conv3 = {
        "conversations": [
            {"from": "human", "value": "h0"},
            {"from": "gpt", "value": "g0"},
            {"from": "human", "value": "h1"},
            {"from": "gpt", "value": "g1"},
            {"from": "human", "value": "h2"},
            {"from": "gpt", "value": "g2"},
        ]
    }
    out = list(fill_sessions(sessions, [conv3]))
    assert len(out) == 3  # 填了 M=3 会话
    assert {r["timestamp_ms"] for r in out} == {10, 11, 12}  # 长会话优先拿到,短会话因耗尽未填


def test_fill_sessions_splices_long_session_from_short():
    # 超长会话 M=4、无单条 ≥4 对话 → 用两条 2 轮拼成 4 轮;prompt 跨拼接边界仍累积
    sessions = [[{"timestamp": 0}, {"timestamp": 1}, {"timestamp": 2}, {"timestamp": 3}]]  # M=4

    def conv2(tag):
        return {
            "conversations": [
                {"from": "human", "value": f"{tag}h0"},
                {"from": "gpt", "value": f"{tag}g0"},
                {"from": "human", "value": f"{tag}h1"},
                {"from": "gpt", "value": f"{tag}g1"},
            ]
        }

    out = list(fill_sessions(sessions, [conv2("A"), conv2("B")]))
    assert len(out) == 4  # 两条 2 轮拼成 4 轮
    assert out[3]["prompt"].startswith(out[0]["prompt"])  # 跨拼接边界仍累积
    assert all(set(r) == {"timestamp_ms", "prompt"} for r in out)


def test_to_turns_configurable_openai_format():
    # 字段 / 角色可配:OpenAI 格式(role/content、user/assistant)
    conv = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "h1"},
        {"role": "assistant", "content": "g1"},
    ]
    system, turns = to_turns(
        conv, role_key="role", text_key="content", user_role="user", assistant_role="assistant"
    )
    assert system == "sys"
    assert turns == [("h1", "g1")]


def test_group_by_session_by_id():
    # trace 自带 session id → 直接按 id 分组,各会话内按 timestamp 排序
    rows = [
        {"session_id": "a", "timestamp": 5},
        {"session_id": "b", "timestamp": 1},
        {"session_id": "a", "timestamp": 2},
    ]
    sessions = group_by_session(rows)
    assert sorted(len(s) for s in sessions) == [1, 2]
    a = next(s for s in sessions if len(s) == 2)
    assert [r["timestamp"] for r in a] == [2, 5]  # 会话内按 timestamp 升序


def test_fill_sessions_custom_turns_fn():
    # 注入 turns_fn 适配不同 content 格式(messages 键 + OpenAI 角色)
    sessions = [[{"timestamp": 0}, {"timestamp": 1}]]
    content = [
        {
            "messages": [
                {"role": "user", "content": "h1"},
                {"role": "assistant", "content": "g1"},
                {"role": "user", "content": "h2"},
                {"role": "assistant", "content": "g2"},
            ]
        }
    ]

    def tf(r):
        return to_turns(
            r["messages"],
            role_key="role",
            text_key="content",
            user_role="user",
            assistant_role="assistant",
        )

    out = list(fill_sessions(sessions, content, turns_fn=tf))
    assert len(out) == 2
    assert out[1]["prompt"].startswith(out[0]["prompt"])
