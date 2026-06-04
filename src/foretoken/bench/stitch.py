"""缝合器:用 Mooncake hash 重建会话骨架,塞真实多轮对话(连贯内容 + 累积复用,配置无关)。

**为什么不复刻 hash 块复用**:Mooncake 的 512 块 hash 是 **Kimi 生产配置(块大小/缓存/内容)特定**
的;我们 vLLM 16 块、GLM、缓存不同,硬对齐它的 512 量化没意义(docs/07 §6.6)。复用本质是内容决定
的(token 级、配置无关)—— 真实多轮对话自己就有真实复用(会话内累积 + 跨会话系统提示)+ 价值区分
(系统提示高 P(reuse)、会话内容中、一次性低)。所以:

    Mooncake hash ──重建──→ 会话(谁是同一会话第几轮 + 每轮 timestamp + 并发)
    真实多轮对话 ──塞──→ 每轮 prompt = 累积前 k 轮(连贯)

产出带 `timestamp_ms` 的 trace → 配 `bench/replay.py` 回放 = KV(对话累积复用)+ MTP(连贯内容)一套全真。

纯数据处理,tokenizer / len_fn 注入;本地可跑可测(真跑 main 需 datasets + GLM tokenizer)。
"""
from __future__ import annotations

import json
import random
from collections.abc import Callable, Iterable, Iterator


def _approx_token_len(text: str) -> int:
    """粗估 token 数(~4 char/token);落地用真 tokenizer 更准(main 里注入)。"""
    return max(1, len(text) // 4)


def to_turns(conversation: list[dict]) -> tuple[str | None, list[tuple[str, str]]]:
    """ShareGPT 对话 → (system, [(user, assistant), ...])。多模态(value 非 str)返回 (None, [])。"""
    system: str | None = None
    turns: list[tuple[str, str]] = []
    pending_user: str | None = None
    for msg in conversation:
        role, val = msg.get("from"), msg.get("value")
        if not isinstance(val, str):
            return None, []  # 多模态 / 非文本,跳过整条
        if role == "system":
            system = val
        elif role == "human":
            pending_user = val
        elif role == "gpt" and pending_user is not None:
            turns.append((pending_user, val))
            pending_user = None
    return system, turns


def reconstruct_sessions(
    trace_rows: Iterable[dict],
    *,
    label_hash_ids: str = "hash_ids",
    label_timestamp: str = "timestamp",
) -> list[list[dict]]:
    """从 Mooncake hash 前缀延续链重建会话(每个会话 = 按时间排序的多轮请求)。

    请求 B 是请求 A 的下一轮 ⟺ A.hash_ids 是 B.hash_ids 的**真前缀**(B 比 A 多尾部块)。
    跨会话共享系统提示**不会误链**:要求"某请求的完整 hash" == B 的前缀,只共享开头不算。
    """
    rows = sorted(trace_rows, key=lambda r: r[label_timestamp])
    sessions: list[list[dict]] = []
    prefix_to_sess: dict[tuple, int] = {}  # 某请求完整 hash → 所属会话 index
    for r in rows:
        h = tuple(r.get(label_hash_ids, []))
        sess: int | None = None
        for n in range(len(h) - 1, 0, -1):  # 最长真前缀优先
            if h[:n] in prefix_to_sess:
                sess = prefix_to_sess[h[:n]]
                break
        if sess is None:
            sessions.append([r])
            sess = len(sessions) - 1
        else:
            sessions[sess].append(r)
        prefix_to_sess[h] = sess
    return sessions


def fill_sessions(
    sessions: list[list[dict]],
    conversations: Iterable[dict],
    *,
    len_fn: Callable[[str], int] = _approx_token_len,
    seed: int = 0,
    sep: str = "\n\n",
    label_timestamp: str = "timestamp",
) -> Iterator[dict]:
    """把真实多轮对话塞进重建的会话槽位。

    对话池先 **shuffle**;每个会话(M 轮)取一个**轮数 ≥ M** 的对话,塞前 M 轮 —— 每轮 prompt =
    累积前 k 轮(连贯,会话内复用自然产生),timestamp = 该轮 Mooncake 时间戳。**大致按轮数对齐,
    不严格对齐长度**(配置无关,本就不该对齐 Mooncake 的 input_length)。
    """
    pool: list[tuple[str | None, list[tuple[str, str]]]] = []
    for rec in conversations:
        system, turns = to_turns(rec.get("conversations", []))
        if turns:
            pool.append((system, turns))
    random.Random(seed).shuffle(pool)

    idx = 0
    for sess in sessions:
        m = len(sess)
        while idx < len(pool) and len(pool[idx][1]) < m:  # 跳过轮数不够的对话
            idx += 1
        if idx >= len(pool):
            break  # 对话池用尽(轮数够的不够多)→ 剩余会话不填
        system, turns = pool[idx]
        idx += 1
        prefix: list[str] = [f"system: {system}"] if system else []
        for k in range(m):
            user, assistant = turns[k]
            yield {
                "timestamp_ms": int(sess[k][label_timestamp]),
                "prompt": sep.join([*prefix, f"user: {user}"]),
                "expected_output_len": len_fn(assistant),
            }
            prefix.append(f"user: {user}")
            prefix.append(f"assistant: {assistant}")


def write_trace_jsonl(rows: Iterable[dict], path: str) -> int:
    """写带 timestamp 的 trace jsonl(给 bench/replay.py 回放);返回条数。"""
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


def _stream_hf(source: str, split: str, limit: int | None) -> Iterator[dict]:
    """流式读 HF 数据集(Mooncake trace / 对话内容)。需要 datasets。"""
    try:
        from datasets import load_dataset
    except ImportError as e:  # pragma: no cover
        raise SystemExit("需要 datasets:pip install datasets(或装 foretoken[server])") from e
    ds = load_dataset(source, split=split, streaming=True)
    for i, row in enumerate(ds):
        if limit is not None and i >= limit:
            break
        yield row
