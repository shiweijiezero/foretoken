import pytest

from foretoken.bench.oracle import belady_hit_rate, pfoo_hit_rate


def test_belady_distinct_then_repeat():
    # 3 distinct，cap 3：前 3 各 miss、后 3 命中 → 3/6
    assert belady_hit_rate([1, 2, 3, 1, 2, 3], capacity=3) == 3 / 6


def test_belady_capacity_one():
    # cap1：仅连续重复命中 → 1(miss)1(hit)2(miss)2(hit)1(miss) = 2/5
    assert belady_hit_rate([1, 1, 2, 2, 1], capacity=1) == 2 / 5


def test_belady_zero_capacity_and_empty():
    assert belady_hit_rate([1, 2, 3], capacity=0) == 0.0
    assert belady_hit_rate([], capacity=4) == 0.0


def test_belady_in_unit_range_and_is_upper_bound():
    accesses = [1, 2, 3, 4, 1, 2, 5, 1, 2, 3, 4, 5]
    hr = belady_hit_rate(accesses, capacity=3)
    assert 0.0 <= hr <= 1.0
    # 容量越大命中率不减(单调性的弱检查)
    assert belady_hit_rate(accesses, 4) >= hr


def test_pfoo_not_implemented_yet():
    with pytest.raises(NotImplementedError):
        pfoo_hit_rate()
