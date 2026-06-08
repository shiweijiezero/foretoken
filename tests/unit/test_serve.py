# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
from foretoken.bench.core.serve import _serve_cmd


def test_serve_cmd_translates_config_and_flags():
    cfg = {
        "model": "ignored",  # 跳过
        "tensor_parallel_size": 2,
        "dtype": "bfloat16",
        "language_model_only": True,  # bool True → 仅 flag
        "disable_log_stats": False,  # bool False → 略
    }
    cmd = _serve_cmd("M", cfg, port=18000, dp=4, api_server_count=2, serve_args=["--enforce-eager"])
    rest = cmd[1:]  # 去掉 vllm 可执行(随环境而异)
    assert rest[:4] == ["serve", "M", "--port", "18000"]
    assert rest[rest.index("--tensor-parallel-size") + 1] == "2"
    assert rest[rest.index("--dtype") + 1] == "bfloat16"
    assert "--language-model-only" in rest
    assert "--disable-log-stats" not in rest  # False 不出
    assert "model" not in rest  # model key 跳过
    assert rest[rest.index("--data-parallel-size") + 1] == "4"
    assert rest[rest.index("--api-server-count") + 1] == "2"
    assert rest[-1] == "--enforce-eager"  # 透传在末尾
