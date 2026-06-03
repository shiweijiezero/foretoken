"""离线最优缓存上界 —— LRU→最优差距的分母(docs/04 门槛零 / MVP 门槛)。

- belady_hit_rate:等大小块的 Belady(MIN)最优替换命中率(精确上界)。
- pfoo_hit_rate:变长对象的离线最优(PFOO,Berger PoMACS'18)——KV 块变长且大,需最小
  费用流近似;此处留接口 + TODO(评测台落地时实现)。
"""
from __future__ import annotations

import bisect
from collections import defaultdict
from collections.abc import Sequence
from typing import Hashable


def belady_hit_rate(accesses: Sequence[Hashable], capacity: int) -> float:
    """Belady MIN 最优替换命中率(等大小对象,容量 = 对象数)。

    驱逐"下一次使用最远(或不再使用)"的对象。O(n*C*log n) —— 脚手架求清晰正确,
    大 trace 可后续优化(堆 / 索引)。
    """
    if capacity <= 0:
        return 0.0
    n = len(accesses)
    if n == 0:
        return 0.0

    positions: dict[Hashable, list[int]] = defaultdict(list)
    for i, k in enumerate(accesses):
        positions[k].append(i)

    def next_use(key: Hashable, after: int) -> float:
        lst = positions[key]
        j = bisect.bisect_right(lst, after)
        return lst[j] if j < len(lst) else float("inf")

    cache: set[Hashable] = set()
    hits = 0
    for i, k in enumerate(accesses):
        if k in cache:
            hits += 1
            continue
        if len(cache) >= capacity:
            victim = max(cache, key=lambda c: next_use(c, i))
            cache.discard(victim)
        cache.add(k)
    return hits / n


def pfoo_hit_rate(*args, **kwargs) -> float:
    """变长对象的离线最优(PFOO)。

    TODO(评测台):min-cost flow 近似,见 docs/04 + Berger PoMACS'18(.research/sources.txt)。
    先用 belady_hit_rate 作等大小块上界。
    """
    raise NotImplementedError(
        "PFOO (variable-size offline optimal) 尚未实现;先用 belady_hit_rate 作等大小块上界。"
    )
