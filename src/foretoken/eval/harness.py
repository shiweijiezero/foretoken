"""4 对照配置 driver(docs/04 / 07):拆出 KV 与 MTP 各自对 goodput 的贡献。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    name: str
    enable_kv: bool       # 价值感知 KV(offloading 插件)
    enable_mtp: bool      # MTP 投机解码
    description: str


# 4 对照配置:全关(原生基线)/ 只开 KV / 只开 MTP / 全开。
CONFIGS: list[Config] = [
    Config("baseline", False, False, "原生 vLLM(LRU APC,无 MTP)—— 基线"),
    Config("kv_only", True, False, "只开价值感知 KV"),
    Config("mtp_only", False, True, "只开 MTP"),
    Config("all_on", True, True, "全开(KV + MTP)"),
]


def run_config(cfg: Config, *, trace_path: str, prompt_pool, slo, gpu_bytes: float):
    """对单个配置:起 / 连 vLLM → 回放负载 → 采集 records → 算 goodput。

    TODO(P0/P1):
    - baseline / mtp_only:vllm serve 原生参数(MTP 走 --speculative-config,见 scripts)。
    - kv_only / all_on:加载 foretoken 的 OffloadingSpec(spec_module_path,docs/02 / 08)。
    - 返回 {goodput, slo_attainment, hit_rate, ...} 供跨配置对比 + 无损校验(docs/13)。
    """
    raise NotImplementedError("TODO: 起 / 连 vLLM + 回放 + 采集 + 算 goodput。")
