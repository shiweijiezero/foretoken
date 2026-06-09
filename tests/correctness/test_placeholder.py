# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""代码正确性测试占位。现以 skip 表达意图,保留目录与 markers,待实现后填实。"""

import pytest


@pytest.mark.lossless
@pytest.mark.skip(reason="无损校验(开优化 == 原生 vLLM,贪心逐 token 等价);需 vLLM")
def test_kv_lossless_greedy_equivalence(): ...


@pytest.mark.determinism
@pytest.mark.skip(reason="确定性(VLLM_BATCH_INVARIANT);A100 可行性待核实")
def test_deterministic_byte_for_byte(): ...
