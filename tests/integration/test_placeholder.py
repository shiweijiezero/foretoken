"""集成测试占位(docs/13)。P1 起填实:插件能被 vLLM 加载并跑通。"""
import pytest


@pytest.mark.slow
@pytest.mark.skip(reason="P1:smoke test —— OffloadingSpec 经 spec_module_path 挂上 vLLM 且不崩")
def test_offloading_spec_loads_on_vllm():
    ...
