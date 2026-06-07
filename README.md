# Foretoken

> Industrial LLM inference on vLLM, with plugin-based optimization and real-workload evaluation.

基于 vLLM 的推理优化项目:在工业级真实推理之上,以树外插件做各项优化(KV 管理、MTP 等),并用真实业务负载检验每一项优化的好坏。不重写引擎、不 fork vLLM,全程经官方扩展点(`OffloadingSpec` / `scheduler_cls` / KV connector / `entry_points`)接入。聚焦 LM 长上下文 / reasoning。

## 功能

| 模块 | 能力 | 状态 |
|---|---|---|
| 评测数据准备 `data_prepare` | Mooncake 真实生产时序 + 真实多轮对话 → 会话重建缝合 → parquet 评测负载 | 已实现,数据集已发布 |
| 回放器 `bench/replay` | 按 trace 时序回放请求、采集 TTFT/TPOT、计算 goodput | 已实现,待 P0 端到端联调 |
| 真实推理基线(P0) | GLM-4.5-Air OpenAI 兼容服务 + 现成 benchmark,测基线 TTFT/TPOT/goodput | 规划 |
| value-aware KV 管理(P1)`plugins/kv_offload` | 按 `P(reuse) × recompute × SLO` 定价的准入 / 驱逐 / TTL,树外 `OffloadingSpec` / `CachePolicy` | 规划 |
| 自适应 MTP(P2)`plugins/spec_decode` | 复用现成 MTP + 自适应 spec 长度控制,量接受率 / 加速 | 规划 |
| goodput 调度 / 非前缀复用 / 多模态(P3+) | 跨 rank 共享、CacheBlend、VLM / VLA / omni | 远期 |

各阶段的"完成"门槛(对标基线、可证伪)见 [`ROADMAP.md`](ROADMAP.md);自建 vs 复用的判定见 [`docs/00`](docs/00-vision-and-modules.md)。

## 真实评测负载

由 Mooncake 真实生产时序 + 真实多轮对话重建缝合而成,已发布为公开数据集,含三个 split(`conversation` / `mooncake` / `toolagent`):

```python
from datasets import load_dataset

ds = load_dataset("weijiezz/foretoken-trace", "conversation")
# 每行:{timestamp_ms, prompt} —— 按真实时序回放即可复现并发与多轮会话结构
```

数据集如何构建、为什么这样构建,见 [`docs/07`](docs/07-eval-playbook.md)。也可本地复现:

```bash
pip install -e '.[dev]'
bash scripts/build_dataset.sh --out-dir <DIR>
```

## 路线图

P0 真实推理基线 → P1 value-aware KV 管理 → P2 自适应 MTP → P3+ goodput 调度 / 多模态。全程树外插件、不 fork,每个模块在"完成"前都要过一个对标基线的可证伪门槛。详见 [`ROADMAP.md`](ROADMAP.md)。

> 真实推理与评测跑在 A100(cu128)服务器;本地(含 Windows / macOS)只装 dev 依赖写代码 + 跑非 GPU 单测——vLLM 无 Windows wheel,GPU 相关跑在服务器。P0 起会提供 `scripts/serve_glm.sh` / `scripts/run_baseline.sh` 的服务与基线入口(当前为占位)。

## 仓库结构

```
src/foretoken/
  data_prepare/      评测数据集构建:Mooncake 时序 + 真实多轮对话 → parquet(已实现)
  bench/replay.py    回放器:按 trace 时序回放、采集 TTFT/TPOT、算 goodput(已实现)
  plugins/           树外插件:kv_offload(P1)/ spec_decode(P2)— 占位,待实现
scripts/             build_dataset(可用)/ serve_glm / run_baseline(待 P0)
docs/                设计文档(00–13 + DESIGN.md)
tests/               unit / integration / correctness(含无损校验 gate)
vendor/vllm/         上游 vLLM 源码(对照扩展点用,不修改;本地 junction,不进 repo)
```

## 文档

| 入口 | 内容 |
|---|---|
| [`ROADMAP.md`](ROADMAP.md) | 执行路线与各阶段门槛 |
| [`docs/00`](docs/00-vision-and-modules.md) | 愿景与模块地图(自建 vs 复用判定) |
| [`docs/04`](docs/04-eval-methodology.md) · [`docs/07`](docs/07-eval-playbook.md) | 评测方法论与实操手册 |
| [`docs/08`](docs/08-vllm-extension-points.md) | vLLM 扩展点(不 fork 集成路径,已源码核实) |

## License

[Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0)。
