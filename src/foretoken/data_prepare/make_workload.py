"""会话重建缝合:由 Mooncake hash 重建会话与时序结构,填入真实多轮对话内容。

不复刻 Mooncake 的 512 块 hash 复用:该粒度取决于 Kimi 生产环境的块大小、缓存与内容,与本项目的
vLLM(16 块)/ GLM 配置不一致(docs/07 §6.6)。复用关系由内容决定、与缓存配置无关——真实多轮对话
本身即产生会话内累积复用与跨会话系统提示共享,并具备价值分层(系统提示复用概率高、会话历史居中、
一次性内容低),即价值感知策略的评测对象。

流程:由 Mooncake hash 的去尾满块前缀延续链重建会话(自适应防误连,详见 reconstruct_sessions),
多个真实 config 合并;为每个会话逐轮填入真实多轮对话,每轮 prompt 为前 k 轮的累积。产出带
`timestamp_ms` 的 trace,供 `bench/replay.py` 回放,在同一份负载上覆盖 KV(会话内累积复用)与
MTP(连贯内容)。

纯数据处理;本地可运行可测试(运行 main 需 datasets)。
"""
from __future__ import annotations

import random
import sys
from collections import Counter
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


def _common_prefix_blocks(rows: list[dict], label_hash_ids: str, threshold: float = 0.5) -> int:
    """公共前缀块数:从开头连续、被 ≥ threshold 请求共享的块数(公共 system/few-shot/工具定义)。"""
    k = 0
    while True:
        vals = [r[label_hash_ids][k] for r in rows if len(r.get(label_hash_ids, [])) > k]
        if not vals or (len(vals) - len(set(vals))) / len(vals) < threshold:
            return k
        k += 1


def reconstruct_sessions(
    trace_rows: Iterable[dict],
    *,
    label_hash_ids: str = "hash_ids",
    label_timestamp: str = "timestamp",
    min_shared_blocks: int | None = None,
) -> list[list[dict]]:
    """从 Mooncake hash 前缀延续链重建会话(每个会话为按时间排序的多轮请求)。

    请求 B 续 A,当 A 的去尾满块前缀(去掉尾部不满的 partial 块)是 B.hash_ids 的前缀。Mooncake 块为
    512 token、input 几乎不是 512 的整数倍,故每请求尾块不满;下一轮累积会把该尾块填满、其 hash
    改变,所以须比去尾满块前缀而非完整 hash——否则前缀在尾块断裂,会把真实多轮漏成单轮。

    防误连:① 共享前缀须 ≥ min_shared_blocks 块(排除仅共享公共 system/few-shot 的不同会话);
    各 config 公共前缀长度不同,min_shared_blocks 默认自适应——自动检测公共前缀块数后取其后一块,
    显式传值则覆盖。② B 的 timestamp 须严格晚于该会话上一轮(排除同时刻并发请求被并入)。
    """
    rows = sorted(trace_rows, key=lambda r: r[label_timestamp])
    if min_shared_blocks is None:
        min_shared_blocks = max(2, _common_prefix_blocks(rows, label_hash_ids) + 1)
    sessions: list[list[dict]] = []
    last_ts: list = []  # 每个会话最后一轮的 timestamp(用于 ② 严格递增约束)
    prefix_to_sess: dict[tuple, int] = {}  # 满块前缀 → 所属会话 index
    for r in rows:
        h = tuple(r.get(label_hash_ids, []))
        ts = r[label_timestamp]
        sess: int | None = None
        for n in range(len(h) - 1, min_shared_blocks - 1, -1):  # 最长前缀优先,≥min_shared 块
            cand = prefix_to_sess.get(h[:n])
            if cand is not None and ts > last_ts[cand]:  # 须严格晚于上一轮,防并发误连
                sess = cand
                break
        if sess is None:
            sessions.append([r])
            sess = len(sessions) - 1
            last_ts.append(ts)
        else:
            sessions[sess].append(r)
            last_ts[sess] = ts
        # 注册「去尾 1 块」到全长的各前缀(容忍 partial 尾块),供后续轮匹配
        for k in range(max(min_shared_blocks, len(h) - 1), len(h) + 1):
            prefix_to_sess[h[:k]] = sess
    return sessions


def fill_sessions(
    sessions: list[list[dict]],
    conversations: Iterable[dict],
    *,
    seed: int = 0,
    sep: str = "\n\n",
    label_timestamp: str = "timestamp",
) -> Iterator[dict]:
    """把真实多轮对话填入重建的会话槽位,长会话优先取较长对话。

    会话按所需轮数 M 降序,每个取「轮数 ≥ M 中最接近 M 的可用对话」:长会话优先抢占稀缺的长对话,
    短会话用充足的短对话。每轮 prompt 为前 k 轮的累积(会话内复用由此产生),timestamp 取自该轮的
    Mooncake 记录。

    读取按需、无固定上限:流式读对话,直到所有会话都可填满(Hall 条件:对每个 t,已读到的轮数 ≥t 的
    对话数 ≥ 需要 ≥t 的会话数)或源耗尽即停——数据足时只读刚够,不足时读尽全源。配不上足够长对话的
    会话用最长可用对话截断填充:产出仍是真实对话的前 k 轮累积(轮数 = 对话轮数 < Mooncake M、少用了
    后几轮 timestamp),非假数据,故不打标记;截断比例打到 stderr 供知悉失真程度。

    只产 timestamp_ms / prompt:输出长度交回放阶段(统一 max_tokens 上限 + 自然 EOS),不预设。
    """
    sess_lens = [len(s) for s in sessions]
    if not sess_lens:
        return
    max_m = max(sess_lens)
    need_cnt = Counter(sess_lens)
    need_ge = [0] * (max_m + 2)  # need_ge[t] = 轮数 ≥ t 的会话数(读取停止条件用)
    for t in range(max_m, 0, -1):
        need_ge[t] = need_ge[t + 1] + need_cnt.get(t, 0)

    # 流式读对话入桶(按轮数分桶);可填满所有会话(Hall 条件)或源耗尽即停
    buckets: dict[int, list] = {}

    def can_fill_all() -> bool:
        have_ge = 0
        for t in range(max_m, 0, -1):
            have_ge += len(buckets.get(t, ()))
            if have_ge < need_ge[t]:
                return False
        return True

    for rec in conversations:
        system, turns = to_turns(rec.get("conversations", []))
        if not turns:
            continue
        buckets.setdefault(len(turns), []).append((system, turns))
        if len(turns) <= max_m and can_fill_all():
            break  # 已能填满所有会话 → 停止读取(数据足时只读刚够)

    rng = random.Random(seed)
    for bucket in buckets.values():  # 桶内打乱,消除原始顺序偏置(同轮数内随机)
        rng.shuffle(bucket)

    # 会话按 M 降序:长会话优先取「≥M 的最小可用对话」;无够长对话则用最长可用对话截断填充
    order = sorted(range(len(sessions)), key=lambda i: len(sessions[i]), reverse=True)
    n_filled = n_truncated = 0
    for si in order:
        sess = sessions[si]
        m = len(sess)
        pick = min((t for t in buckets if t >= m and buckets[t]), default=None)
        if pick is None:  # 无 ≥M 对话 → 用最长可用对话截断填充
            pick = max((t for t in buckets if buckets[t]), default=None)
            if pick is None:
                break  # 对话池耗尽
            n_truncated += 1
        system, turns = buckets[pick].pop()
        n_filled += 1
        prefix: list[str] = [f"system: {system}"] if system else []
        for k in range(min(m, len(turns))):  # 正常 = m;截断 = 对话轮数 < m
            user, assistant = turns[k]
            yield {
                "timestamp_ms": int(sess[k][label_timestamp]),
                "prompt": sep.join([*prefix, f"user: {user}"]),
            }
            prefix.append(f"user: {user}")
            prefix.append(f"assistant: {assistant}")

    if n_filled:
        print(
            f"[make_workload] 截断填充 {n_truncated}/{n_filled} 会话 "
            f"({100 * n_truncated / n_filled:.1f}%,无足够长对话、用较短对话截断;增大或更换 content 源可降低)",
            file=sys.stderr,
        )


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
