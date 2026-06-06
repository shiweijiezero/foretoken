"""一等测试占位(docs/13 L1)。P1 起代码后填实;现在用 skip 表达意图、保留目录与 markers。"""

import pytest


@pytest.mark.lossless
@pytest.mark.skip(reason="P1:无损校验(开优化 == 原生 vLLM,贪心逐 token 等价);需 vLLM")
def test_kv_lossless_greedy_equivalence(): ...


@pytest.mark.determinism
@pytest.mark.skip(reason="P0/P1:确定性(VLLM_BATCH_INVARIANT);A100 可行性待核实,见 docs/10")
def test_deterministic_byte_for_byte(): ...
