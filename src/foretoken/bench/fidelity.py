"""门槛零:回放保真(沉默的杀手,docs/04 / 07)。

离线用 Mooncake trace 的 hash_ids 模拟块级前缀缓存 + LRU,算命中率;与实测原生 vLLM
APC 命中率比对。两者接近 = 回放正确,后续策略对比才可信。

vLLM APC 语义(docs/02):块级、严格最长前缀、首次未命中即停止匹配;LRU 驱逐。
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from typing import Hashable


def offline_prefix_hit_rate(
    requests: Sequence[Sequence[Hashable]],
    capacity_blocks: int,
) -> float:
    """块级前缀缓存 + LRU 的离线 block 命中率。

    requests:每个请求是一个有序 block-id 序列(Mooncake ``hash_ids``)。
    capacity_blocks:缓存容量(block 数)。
    返回:命中 block / 总 block。
    """
    if capacity_blocks <= 0:
        return 0.0
    cache: OrderedDict[Hashable, None] = OrderedDict()
    total = 0
    hits = 0
    for req in requests:
        prefix_live = True
        for b in req:
            total += 1
            if prefix_live and b in cache:
                hits += 1
                cache.move_to_end(b)
            else:
                prefix_live = False  # 首次 miss 后前缀断,后续不计前缀命中
                if b in cache:
                    cache.move_to_end(b)
                else:
                    cache[b] = None
                    if len(cache) > capacity_blocks:
                        cache.popitem(last=False)  # 驱逐 LRU
    return hits / total if total else 0.0


def gate_zero_ok(offline_hit: float, measured_hit: float, tol: float = 0.02) -> bool:
    """门槛零判定:离线与实测 APC 命中率之差 <= tol(默认 2 个百分点)。"""
    return abs(offline_hit - measured_hit) <= tol
