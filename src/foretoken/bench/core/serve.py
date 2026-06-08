# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
"""自起 / 关停 `vllm serve` 子进程:就绪后供 API 形式回放,退出时整组 kill 释放 GPU。

整组回收(`start_new_session` + `killpg`):vllm serve 会派生 EngineCore / 前端 worker 子进程,
只杀父进程会留孤儿占卡。POSIX only(serve 跑在 GPU 机器,即 Linux)。
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def _vllm_exe() -> str:
    """venv 内的 vllm 可执行(同 sys.executable 目录);缺失回退 PATH 上的 vllm。"""
    cand = Path(sys.executable).with_name("vllm")
    return str(cand) if cand.exists() else "vllm"


def _serve_cmd(
    model: str,
    serve_cfg: dict,
    *,
    port: int,
    dp: int | None,
    api_server_count: int | None,
    serve_args: list[str],
) -> list[str]:
    """拼 `vllm serve` 命令:[serve] 字段转 flag + dp / 前端数 / 透传参数。"""
    cmd = [_vllm_exe(), "serve", model, "--port", str(port)]
    for k, v in serve_cfg.items():
        if k == "model":
            continue
        flag = "--" + k.replace("_", "-")
        if v is True:
            cmd.append(flag)
        elif v is not False:
            cmd += [flag, str(v)]
    if dp:
        cmd += ["--data-parallel-size", str(dp)]
    if api_server_count:
        cmd += ["--api-server-count", str(api_server_count)]
    return cmd + list(serve_args)


def _wait_healthy(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            with urllib.request.urlopen(url, timeout=2) as r:  # noqa: S310  本机 health
                if r.status == 200:
                    return
        time.sleep(1.0)
    raise RuntimeError(f"vllm serve 未在 {timeout:.0f}s 内就绪:{url}")


def _terminate(proc: subprocess.Popen, grace: float = 20.0) -> None:
    if proc.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)  # 整组先礼
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)  # 不退则后兵


@contextlib.contextmanager
def vllm_serve(
    model: str,
    serve_cfg: dict,
    *,
    port: int = 18000,
    dp: int | None = None,
    api_server_count: int | None = None,
    serve_args: list[str] | None = None,
    ready_timeout: float = 900.0,
):
    """起 vllm serve(新进程组),就绪后 yield endpoint;退出整组 SIGTERM→SIGKILL 释放 GPU。"""
    cmd = _serve_cmd(
        model, serve_cfg, port=port, dp=dp,
        api_server_count=api_server_count, serve_args=serve_args or [],
    )
    print("起 vllm serve:", " ".join(cmd))
    proc = subprocess.Popen(cmd, start_new_session=True)  # 独立进程组,便于整组回收
    try:
        _wait_healthy(f"http://localhost:{port}/health", ready_timeout)
        print(f"vllm serve 就绪:http://localhost:{port}")
        yield f"http://localhost:{port}"
    finally:
        _terminate(proc)
        print("vllm serve 已关停(GPU 释放)")
