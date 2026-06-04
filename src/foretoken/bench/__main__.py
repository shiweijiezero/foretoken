"""python -m foretoken.bench 入口(薄壳,转 run_evaluation)。"""
from foretoken.bench.run import run_evaluation  # noqa: F401  (re-export 供外部 import)

if __name__ == "__main__":
    raise SystemExit(
        "TODO(P0):接 CLI 参数(--trace / --prompt-pool / --slo / --gpu-bytes / --capacity-blocks)"
        "后启用 run_evaluation;当前评测流水线依赖 vLLM,见 run.py 与各模块 TODO。"
    )
