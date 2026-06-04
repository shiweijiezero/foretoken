import pytest

from foretoken.bench.fidelity import gate_zero_ok, offline_prefix_hit_rate

pytestmark = pytest.mark.eval


def test_shared_prefix_hits():
    # 两请求共享前缀 [1,2,3];第二请求前 3 个全命中 → 命中 3 / 总 7
    assert offline_prefix_hit_rate([[1, 2, 3], [1, 2, 3, 4]], capacity_blocks=100) == 3 / 7


def test_prefix_breaks_on_first_miss():
    # 第二请求 [1,9,3]:1 命中、9 miss → 前缀断,后面的 3 不计前缀命中 → 1 / 6
    assert offline_prefix_hit_rate([[1, 2, 3], [1, 9, 3]], capacity_blocks=100) == 1 / 6


def test_lru_eviction_under_small_capacity():
    # cap2:req2 把 req1 的块挤掉 → req3 无命中
    assert offline_prefix_hit_rate([[1, 2], [3, 4], [1, 2]], capacity_blocks=2) == 0.0


def test_zero_capacity():
    assert offline_prefix_hit_rate([[1, 2, 3]], capacity_blocks=0) == 0.0


def test_gate_zero_tolerance():
    assert gate_zero_ok(0.50, 0.515, tol=0.02)
    assert not gate_zero_ok(0.50, 0.55, tol=0.02)
