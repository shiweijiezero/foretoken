import pytest

from foretoken.bench.stitch import build_block_pool, extract_texts, fill_mooncake_trace


def test_extract_texts_skips_nonstring():
    records = [
        {"conversations": [{"from": "human", "value": "hi"}, {"from": "gpt", "value": "yo"}]},
        {"conversations": [{"from": "human", "value": [{"image": "x"}]}, {"from": "gpt", "value": "desc"}]},
    ]
    # 多模态的 list value 跳过,同记录的纯文本仍保留
    assert list(extract_texts(records)) == ["hi", "yo", "desc"]


def test_build_block_pool_chunks():
    # 2 段 × 3 token = 6 token,block_size 2 → 3 块
    pool = build_block_pool(["a", "b"], encode=lambda s: [1, 2, 3], block_size=2)
    assert [len(b) for b in pool] == [2, 2, 2]


def test_fill_mooncake_trace_reuse_alignment():
    pool = build_block_pool(["unused"], encode=lambda s: list(range(2000)), block_size=512)
    assert len(pool) >= 3
    decode = lambda ids: ",".join(map(str, ids))  # noqa: E731
    rows = [
        {"timestamp": 0, "input_length": 1024, "output_length": 10, "hash_ids": [1, 2]},
        {"timestamp": 5000, "input_length": 1536, "output_length": 7, "hash_ids": [1, 2, 3]},
    ]
    out = list(fill_mooncake_trace(rows, pool, decode))
    # ★ 相同 hash 前缀 [1,2] → 第二行严格以第一行为前缀(复用 100% 对齐 Mooncake)
    assert out[1]["prompt"].startswith(out[0]["prompt"])
    p0, p1 = out[0]["prompt_token_ids"], out[1]["prompt_token_ids"]
    assert p1[: len(p0)] == p0  # token 级前缀(回放优先用 token_ids,边界精确)
    # 真实时序 + 输出长度从 Mooncake 透传
    assert out[0]["timestamp_ms"] == 0
    assert out[1]["timestamp_ms"] == 5000
    assert out[1]["expected_output_len"] == 7


def test_fill_empty_pool_raises():
    with pytest.raises(ValueError):
        list(fill_mooncake_trace([{"timestamp": 0, "input_length": 1, "hash_ids": [1]}], [], lambda x: ""))
