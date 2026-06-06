# Foretoken

> **Value-aware KV management, MTP control, and goodput-driven evaluation — built on vLLM, zero-fork.**

**状态:** pre-P0(设计完成,即将进入真实推理基线) · 基于 vLLM 树外插件(不 fork) · Apache-2.0

Foretoken 是一个基于 vLLM 的推理优化项目。它不重写引擎、不 fork vLLM,而是全程通过 vLLM 的官方扩展点(`OffloadingSpec` / `scheduler_cls` / KV connector / `entry_points`)接入,在工业级真实推理之上,把 KV-cache 当作按价值定价的一等资源来管理、用自适应 MTP 投机解码,并用真实评测检验每一项优化。当前从单人维护起步,聚焦 LM 长上下文 / reasoning 场景。

完整动机见 [`docs/00`](docs/00-vision-and-modules.md),执行路线见 [`ROADMAP.md`](ROADMAP.md)。

## 三件事

并列的三个支柱——主体、手段、裁判:

- **真实推理(主体)** — 复用 vLLM 把大型 Dense/MoE + 真实业务负载在多节点、高并发下跑好、能服务。这是地基,也是第一个交付目标(P0)。
- **优化(手段)** — value-aware **KV 管理**(按 `P(reuse) × recompute × SLO` 定价的准入 / 驱逐 / TTL,最大也最难的一块)、自适应 **MTP** 投机控制、**goodput 调度**。全程树外插件,不 fork 核心。
- **评测(裁判)** — 真模型现场生成 + 真实业务负载 + Mooncake 真实时序,对标原生 LRU / LMCache 等竞争者,做无损校验(输出 == 原生)。每个优化模块在“完成”前都要过一个可证伪门槛。统一标尺:SLO 约束下的每 GPU 字节秒 goodput。

## 状态

设计与文档梳理完成,环境就绪(最新 vLLM 已 clone、A100 集群可用),**即将进入 P0(真实推理基线)**。P0 不依赖任何升级,空卡 + 用户态即可起步。各阶段门槛见 [`ROADMAP.md`](ROADMAP.md)。

## 快速开始

> 真实推理与评测跑在 A100(cu128)服务器;本地(含 Windows / macOS)只装 dev 依赖写代码 + 跑非 GPU 单测——vLLM 无 Windows wheel,GPU 相关跑在服务器。

**本地开发**

```bash
pip install -e '.[dev]'
pytest                       # 非 GPU 单测
```

**在 A100 上跑 P0 基线**

```bash
# 1. 装带 vLLM 的服务器依赖(cu128 / A100)
pip install -e '.[server]'

# 2. 起 GLM-4.5-Air 的 OpenAI 兼容服务(8×A100 80GB,TP=8,bf16)
MODEL_PATH=<权重目录> HF_HOME=<缓存目录> bash scripts/serve_glm.sh

# 3. 跑 benchmark,测 TTFT / TPOT / goodput
bash scripts/run_baseline.sh

# (可选)由 Mooncake 时序 + 真实多轮对话构建可复现评测数据集
bash scripts/build_dataset.sh --out-dir <DIR>
```

各脚本的必填环境变量见脚本头部注释;vLLM 参数以 `vllm serve --help` / `vllm bench serve --help` 为准。

## 仓库结构

```
src/foretoken/
  bench/replay.py       回放器:按 trace 时序回放、采集 TTFT/TPOT、算 goodput
  data_prepare/         评测数据集构建:Mooncake 时序 + 真实多轮对话 → parquet
  plugins/              树外插件:kv_offload(P1)/ spec_decode(P2)— 占位,待实现
scripts/                serve_glm / run_baseline / build_dataset
docs/                   设计文档(00–13 + DESIGN.md)
tests/                  unit / integration / correctness(含无损校验 gate)
vendor/vllm/            上游 vLLM 源码(对照扩展点用,不修改)
```

## 文档

| 入口 | 内容 |
|---|---|
| [`ROADMAP.md`](ROADMAP.md) | 执行路线与各阶段门槛 |
| [`docs/00`](docs/00-vision-and-modules.md) | 愿景与模块地图(自建 vs 复用判定) |
| [`docs/04`](docs/04-eval-methodology.md) · [`docs/07`](docs/07-eval-playbook.md) | 评测方法论与实操手册 |
| [`docs/08`](docs/08-vllm-extension-points.md) | vLLM 扩展点(不 fork 集成路径,已源码核实) |

完整设计文档见 [`docs/`](docs/)。

## 非目标

不从零造引擎,不做跨引擎层(单引擎 vLLM),不做通用控制平面,不做新的传输 fabric / 卸载层(挂载 NIXL / Mooncake / LMCache)。确定性复用现成的(`VLLM_BATCH_INVARIANT`),不重造。

## License

[Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0)。
