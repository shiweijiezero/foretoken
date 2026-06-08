# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""会话重建:由 Mooncake hash 重建会话与时序结构,填入真实多轮对话内容。

不复刻 Mooncake 的 512 块 hash 复用:该粒度取决于 Kimi 生产环境的块大小、缓存与内容,与本项目的
vLLM(16 块)/ GLM 配置不一致(docs/07 §6.6)。复用关系由内容决定、与缓存配置无关——真实多轮对话
本身即产生会话内累积复用与跨会话系统提示共享,并具备价值分层(系统提示复用概率高、会话历史居中、
一次性内容低),即价值感知策略的评测对象。

流程:由 Mooncake hash 的去尾满块前缀延续链重建会话(自适应防误连,详见 reconstruct_sessions);
为每个会话逐轮填入真实多轮对话,每轮 prompt 为前 k 轮的累积。产出带 `timestamp_ms` 的 trace,供
`bench/replay.py` 回放,覆盖 KV(会话内累积复用)与 MTP(连贯内容)。多个真实 config 各作为一个
split(build_dataset),保留各自真实时序、互不串联。

纯数据处理逻辑(to_turns / reconstruct_sessions 等)用标准库实现、可单测;读 HF 数据集走 datasets。
"""

from __future__ import annotations

import random
import sys
from collections.abc import Callable, Iterable, Iterator

from datasets import load_dataset


def to_turns(
    conversation: list[dict],
    *,
    role_key: str = "from",
    text_key: str = "value",
    system_role: str = "system",
    user_role: str = "human",
    assistant_role: str = "gpt",
) -> tuple[str | None, list[tuple[str, str]]]:
    """对话 → (system, [(user, assistant), ...])。字段 / 角色可配以适配不同格式。

    默认 ShareGPT;OpenAI 格式传 role_key="role"、text_key="content"、user_role="user"、
    assistant_role="assistant"。多模态(text 非 str)返回 (None, [])。
    """
    system: str | None = None
    turns: list[tuple[str, str]] = []
    pending_user: str | None = None
    for msg in conversation:
        role, val = msg.get(role_key), msg.get(text_key)
        if not isinstance(val, str):
            return None, []  # 多模态 / 非文本,跳过整条
        if role == system_role:
            system = val
        elif role == user_role:
            pending_user = val
        elif role == assistant_role and pending_user is not None:
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


def group_by_session(
    trace_rows: Iterable[dict],
    *,
    label_session: str = "session_id",
    label_timestamp: str = "timestamp",
) -> list[list[dict]]:
    """trace 自带 session/user id 时,直接按 id 分组重建会话(各会话内按 timestamp 排序)。

    用于带显式会话标识的 trace(如 BurstGPT);Mooncake 无 id,改用 reconstruct_sessions(hash 重建)。
    两者均返回 list[list[dict]],fill_sessions 通用接收。
    """
    groups: dict = {}
    for r in trace_rows:
        groups.setdefault(r[label_session], []).append(r)
    return [sorted(g, key=lambda r: r[label_timestamp]) for g in groups.values()]


def fill_sessions(
    sessions: list[list[dict]],
    conversations: Iterable[dict],
    *,
    seed: int = 0,
    label_timestamp: str = "timestamp",
    turns_fn: Callable[[dict], tuple[str | None, list[tuple[str, str]]]] | None = None,
) -> Iterator[dict]:
    """填充会话槽位,每轮产出 `{session_id, turn, timestamp_ms, user, assistant, system}`。

    session_id 为会话下标(回放按它分组跑闭环);assistant 为预录答案,仅留作对照,闭环回放改用模型现场
    生成。会话按轮数 M 降序:优先单条「轮数 ≥ M」对话,否则拼多条凑够 M。turns_fn 默认 ShareGPT。
    """
    if turns_fn is None:

        def turns_fn(rec: dict) -> tuple[str | None, list[tuple[str, str]]]:
            return to_turns(rec.get("conversations", []))

    sess_lens = [len(s) for s in sessions]
    if not sess_lens:
        return
    total_need = sum(sess_lens)

    # 流式读对话入桶(按轮数分桶);累计轮数够覆盖所有会话(拼接可填)或源耗尽即停
    buckets: dict[int, list] = {}
    have_turns = 0
    for rec in conversations:
        system, turns = turns_fn(rec)
        if not turns:
            continue
        buckets.setdefault(len(turns), []).append((system, turns))
        have_turns += len(turns)
        if have_turns >= total_need:
            break  # 总轮数够 → 拼接能填满所有会话

    rng = random.Random(seed)
    for bucket in buckets.values():  # 桶内打乱,消除原始顺序偏置(同轮数内随机)
        rng.shuffle(bucket)

    # 会话按 M 降序:长会话优先。单条 ≥M 对话用单条(连贯);否则拼接多条凑够 M
    order = sorted(range(len(sessions)), key=lambda i: len(sessions[i]), reverse=True)
    n_filled = n_spliced = 0
    for si in order:
        sess = sessions[si]
        m = len(sess)
        pick = min((t for t in buckets if t >= m and buckets[t]), default=None)
        if pick is not None:  # 单条 ≥M 对话(完全连贯)
            system, turns = buckets[pick].pop()
        else:  # 拼接多条短对话凑够 M(超长会话)
            system, turns = None, []
            while len(turns) < m:
                avail = max((t for t in buckets if buckets[t]), default=None)
                if avail is None:
                    break  # 对话池耗尽
                sys_i, turns_i = buckets[avail].pop()
                if system is None:
                    system = sys_i  # 用第一条对话的 system
                turns.extend(turns_i)
            if not turns:
                break  # 对话池耗尽
            n_spliced += 1
        n_filled += 1
        for k in range(min(m, len(turns))):  # 单条/拼接均凑够 m(除非源耗尽)
            user, assistant = turns[k]  # assistant 仅留作对照,闭环回放用模型现场生成
            yield {
                "session_id": si,
                "turn": k,
                "timestamp_ms": int(sess[k][label_timestamp]),
                "user": user,
                "assistant": assistant,
                "system": system if k == 0 else None,
            }

    if n_filled:
        print(
            f"[make_workload] 拼接填充 {n_spliced}/{n_filled} 会话 "
            f"({100 * n_spliced / n_filled:.1f}%,超长会话用多条对话拼接,接头处 MTP 略降)",
            file=sys.stderr,
        )


def _stream_hf(
    source: str, split: str, limit: int | None, *, config: str | None = None
) -> Iterator[dict]:
    """流式读 HF 数据集(Mooncake trace / 对话内容)。
    config 用于含多 config 的数据集
    """
    ds = load_dataset(source, config, split=split, streaming=True)
    for i, row in enumerate(ds):
        if limit is not None and i >= limit:
            break
        yield row
