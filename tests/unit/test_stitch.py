from foretoken.bench.stitch import expand_conversation, stitch_dataset


def _wc(s: str) -> int:
    return max(1, len(s.split()))


def test_multi_turn_accumulates_prefix():
    conv = [
        {"from": "human", "value": "hi"},
        {"from": "gpt", "value": "hello there"},
        {"from": "human", "value": "more"},
        {"from": "gpt", "value": "ok sure"},
    ]
    reqs = expand_conversation(conv, "s0", len_fn=_wc)
    assert len(reqs) == 2
    assert (reqs[0].turn, reqs[1].turn) == (1, 2)
    # ★ 真实复用:turn2 的 prompt 严格以 turn1 的 prompt 为前缀
    assert reqs[1].prompt.startswith(reqs[0].prompt)
    # expected_output_len = 该轮 gpt 回复长度
    assert reqs[0].expected_output_len == _wc("hello there")
    assert reqs[1].expected_output_len == _wc("ok sure")


def test_system_prefix_included():
    conv = [
        {"from": "system", "value": "you are helpful"},
        {"from": "human", "value": "q1"},
        {"from": "gpt", "value": "a1"},
    ]
    reqs = expand_conversation(conv, "s0", len_fn=_wc)
    assert len(reqs) == 1
    assert reqs[0].prompt.startswith("system: you are helpful")
    assert "user: q1" in reqs[0].prompt


def test_multimodal_conversation_skipped():
    conv = [
        {"from": "human", "value": [{"type": "image_url", "image_url": {"url": "x"}}]},
        {"from": "gpt", "value": "desc"},
    ]
    assert expand_conversation(conv, "s0") == []


def test_dangling_human_yields_no_request():
    conv = [
        {"from": "human", "value": "h1"},
        {"from": "gpt", "value": "g1"},
        {"from": "human", "value": "h2-no-reply"},
    ]
    reqs = expand_conversation(conv, "s0", len_fn=_wc)
    assert len(reqs) == 1  # 只有成对的 h1/g1 产请求


def test_stitch_dataset_numbers_sessions():
    records = [
        {"conversations": [{"from": "human", "value": "a"}, {"from": "gpt", "value": "b"}]},
        {"conversations": [{"from": "human", "value": "c"}, {"from": "gpt", "value": "d"}]},
    ]
    reqs = list(stitch_dataset(records, len_fn=_wc))
    assert {r.session_id for r in reqs} == {"s0", "s1"}
    assert len(reqs) == 2
    assert all(r.timestamp_ms is None for r in reqs)  # superset 字段默认空,B 路径再填
