"""会话重建缝合:由 Mooncake hash 重建会话与时序结构,填入真实多轮对话内容。

不复刻 Mooncake 的 512 块 hash 复用:该粒度取决于 Kimi 生产环境的块大小、缓存与内容,与本项目的
vLLM(16 块)/ GLM 配置不一致(docs/07 §6.6)。复用关系由内容决定、与缓存配置无关——真实多轮对话
本身即产生会话内累积复用与跨会话系统提示共享,并具备价值分层(系统提示复用概率高、会话历史居中、
一次性内容低),即价值感知策略的评测对象。

流程:由 Mooncake hash 重建会话(同一会话的轮次顺序、每轮 timestamp、并发);为每个会话逐轮填入
真实多轮对话,每轮 prompt 为前 k 轮的累积。产出带 `timestamp_ms` 的 trace,供 `bench/replay.py`
回放,在同一份负载上覆盖 KV(会话内累积复用)与 MTP(连贯内容)。

纯数据处理;本地可运行可测试(运行 main 需 datasets)。
"""
from __future__ import annotations

import random
from collections.abc import Iterable, Iterator


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
    """从 Mooncake hash 前缀延续链重建会话(每个会话为按时间排序的多轮请求)。

    请求 B 是请求 A 的下一轮,当且仅当 A.hash_ids 是 B.hash_ids 的真前缀(B 比 A 多尾部块)。
    跨会话共享系统提示不会误并:要求某请求的完整 hash 等于 B 的前缀,仅共享开头不视为同一会话。
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
    seed: int = 0,
    sep: str = "\n\n",
    label_timestamp: str = "timestamp",
) -> Iterator[dict]:
    """把真实多轮对话填入重建的会话槽位,尽量减少数据浪费。

    会话按所需轮数 M 升序、对话池按轮数升序配对(双指针):每个会话取轮数 ≥ M 中最接近 M 的对话。
    这样短对话优先供短会话(不被长会话跳过丢弃),长对话留给长会话(多出的轮次最少)。每轮 prompt
    为前 k 轮的累积(会话内复用由此自然产生),timestamp 取自该轮的 Mooncake 记录。长度按轮数粗对齐,
    不严格对齐 Mooncake 的 input_length(配置无关)。

    只产 timestamp_ms 与 prompt:输出长度交回放阶段(统一 max_tokens 上限 + 自然 EOS),不预设。
    """
    pool: list[tuple[str | None, list[tuple[str, str]]]] = []
    for rec in conversations:
        system, turns = to_turns(rec.get("conversations", []))
        if turns:
            pool.append((system, turns))
    random.Random(seed).shuffle(pool)  # 先打乱,消除原始顺序偏置
    pool.sort(key=lambda st: len(st[1]))  # 再按轮数升序(稳定排序,同轮数内仍随机)

    # 会话按 M 升序处理;双指针保证短对话不被长会话跳过丢弃
    order = sorted(range(len(sessions)), key=lambda i: len(sessions[i]))
    idx = 0
    for si in order:
        sess = sessions[si]
        m = len(sess)
        while idx < len(pool) and len(pool[idx][1]) < m:  # 轮数不足者已供更短会话或无处可用
            idx += 1
        if idx >= len(pool):
            break  # 轮数足够的对话已用尽 → 剩余(更长)会话不填
        system, turns = pool[idx]
        idx += 1
        prefix: list[str] = [f"system: {system}"] if system else []
        for k in range(m):
            user, assistant = turns[k]
            yield {
                "timestamp_ms": int(sess[k][label_timestamp]),
                "prompt": sep.join([*prefix, f"user: {user}"]),
            }
            prefix.append(f"user: {user}")
            prefix.append(f"assistant: {assistant}")


def _stream_hf(
    source: str, split: str, limit: int | None, *, config: str | None = None
) -> Iterator[dict]:
    """流式读 HF 数据集(Mooncake trace / 对话内容)。config 用于含多 config 的数据集。需要 datasets。"""
    try:
        from datasets import load_dataset
    except ImportError as e:  # pragma: no cover
        raise SystemExit("需要 datasets:pip install datasets(或装 foretoken[server])") from e
    ds = load_dataset(source, config, split=split, streaming=True)
    for i, row in enumerate(ds):
        if limit is not None and i >= limit:
            break
        yield row
