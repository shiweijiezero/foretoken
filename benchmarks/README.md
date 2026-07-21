# Foretoken Bench

面向已部署 **vLLM / OpenAI 兼容 API** 的 LLM 压测工具。

入口：`foretoken bench`（入口 `[cli.py](./cli.py)`，选项与解析见 `[config.py](./config.py)`）。设计与路线图见 `[prd.md](./prd.md)`。

---

## 安装

```bash
cd foretoken
pip install -e .
# 使用 EvalScope perf 数据集插件（openqa / share_gpt / random+tokenizer 等）时：
pip install -e '.[evalscope]'   # 或 pip install evalscope
foretoken --help
foretoken bench --help
```

---

## 快速开始

```bash
# 单次压测（默认 --dataset openqa）
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B \
  --parallel 4 \
  --number 20 \
  --max-tokens 64
```

必填：`--url`、`--model`。

---

## 命令形态

主入口是**一个命令 + 参数组合**，不是多套业务子命令：

```bash
foretoken bench [OPTIONS]
```

历史兼容（可选）：`foretoken bench run` / `foretoken bench sweep` 仍转发到同一套参数。

路由逻辑（由参数决定模式）：


| 条件 | 行为 | 状态 |
| --- | --- | --- |
| 默认 | 单次压测 | 可用 |
| `--parallel` / `--number` / `--rate` 为列表 | 多点扫描 + Pareto（tok/s/user vs tok/s/GPU） | 可用 |
| `--rate > 0`（无 `--open-loop`） | 闭环 + 泊松 pacing（EvalScope `ClosedLoopStrategy`） | 可用 |
| `--open-loop`（可与 `--rate` 组合） | 开环（`parallel=-1`，EvalScope `OpenLoopStrategy`）；不可与多值 `--parallel` 同用 | 可用 |
| `--bench-params` / `--serve-params` | vLLM 风格多组合扫参（笛卡尔积）；优先于列表 sweep | 可用 |
| `--dataset` 插件名 + `--dataset-path` / `--prompt` | 单数据集加载 | 可用 |
| `--dataset sequential\|mixed` + 多路径 `--dataset-path` | 多数据集调度 | 规划中（Phase 3） |
| `--sla-auto-tune` | SLA 二分最大并发 | 规划中（Phase 2） |
| `--eval-suite general\|tool\|both` | 正确性评测 | 规划中（Phase 0.5） |


---

## 常用参数

### 连接


| 参数          | 说明                             | 默认     |
| ----------- | ------------------------------ | ------ |
| `--url`     | OpenAI 兼容 chat completions URL | **必填** |
| `--model`   | 模型名                            | **必填** |
| `--api-key` | API Key（可选）                    | 空      |
| `--timeout` | 请求超时（秒）                        | `300`  |


### 负载


| 参数              | 说明                                                                                             | 默认    |
| --------------- | ---------------------------------------------------------------------------------------------- | ----- |
| `--parallel`    | 并发；`8` 或 `1,2,4,8`（列表即 sweep）                                                                  | `1`   |
| `--number`      | 请求数；可与 parallel / rate 对齐成列表                                                                   | `100` |
| `--rate`        | 到达率（req/s）。`-1` = 无 pacing；`>0` = **泊松 pacing**（EvalScope 绝对时间调度）。默认仍是闭环（semaphore=`parallel`） | `-1`  |
| `--open-loop`   | 开环：不限并发（EvalScope `parallel=-1`）；可与 `--rate` 组合做泊松 pacing；闭环（默认）用 semaphore=`parallel`         | False |
| `--max-tokens`  | 生成长度                                                                                           | `128` |
| `--temperature` | 采样温度                                                                                           | `0`   |
| `--stream`      | 流式请求以统计 TTFT/TPOT                                                                              | True  |
| `--gpu-count`   | Pareto 纵坐标 tokens/s/GPU 的 GPU 分母                                                               | `1`   |


Sweep / 参数扫参结束后自动画 Pareto：**X = tokens/s/user**，**Y = tokens/s/GPU**（对齐 vLLM；有 vLLM 时复用官方绘图）。

- **列表 sweep**（`--parallel` / `--number` / `--rate` 为列表）：图在 `artifacts/pareto.png`，前沿在 `summary.json` 的 `pareto_frontier`。
- **参数组合扫参**（`--bench-params` / `--serve-params`）：图在实验根目录 `pareto.png`，另有 `pareto_frontier.json` 与 `pareto/by_*/` 分组图。

### 数据

复用 `evalscope.perf` 的 `DatasetRegistry` 插件，参数语义与 `evalscope perf` 一致。


| 参数 | 说明 | 默认 |
| --- | --- | --- |
| `--dataset` | **数据集模式**：① EvalScope 插件名 `openqa` / `random` / `line_by_line` / `custom_multi_turn` / …；② 多数据集调度 `sequential` / `mixed`（需多路径 `--dataset-path`，Phase 3） | `openqa` |
| `--dataset-path` | **数据集文件或目录路径**；支持逗号分隔列表。单路径配合插件模式；多路径配合 `--dataset sequential\|mixed`（Phase 3） | 空 |
| `--dataset-offset` | random 模式的 token 序列偏移 | `0` |
| `--tokenizer-path` | Tokenizer 路径；`--dataset random` 时**必需** | 空 |
| `--min-prompt-length` | 最小 prompt 长度（有 tokenizer 时按 token） | `0` |
| `--max-prompt-length` | 最大 prompt 长度 | `131072` |
| `--prefix-length` | random 共享前缀长度 | `0` |
| `--apply-chat-template` | 是否包成 chat messages；默认按 URL 是否为 `chat/completions` 自动判断 | 自动 |
| `--prompt` | 固定 prompt（设置后覆盖 dataset） | 空 |
| `--max-turns` | `custom_multi_turn` 截断到前 N 个 user turn | 不截断 |


常用组合：

```bash
# openqa（默认模式，按需从 ModelScope 下载；一般无需 --dataset-path）
foretoken bench --url ... --model ... --dataset openqa --number 50

# conversation.jsonl：模式 + 路径（路径相对当前工作目录；仓库根目录下有该文件）
foretoken bench --url ... --model ... \
  --dataset custom_multi_turn --dataset-path conversation.jsonl \
  --max-turns 4 --number 20

# 普通 JSONL / 文本：模式 line_by_line + 路径（prompts.txt 需自行准备）
foretoken bench --url ... --model ... \
  --dataset line_by_line --dataset-path prompts.txt

# random（必须带 --tokenizer-path）
foretoken bench --url ... --model ... \
  --dataset random --tokenizer-path /path/to/tokenizer \
  --min-prompt-length 128 --max-prompt-length 512 --prefix-length 32


# 多数据集（Phase 3：--dataset 取 sequential|mixed）
foretoken bench --url ... --model ... \
  --dataset sequential \
  --dataset-path conversation.jsonl,toolagent.jsonl
foretoken bench --url ... --model ... \
  --dataset mixed \
  --dataset-path conversation.jsonl,toolagent.jsonl
```

> `--dataset random` **必须**同时传 `--tokenizer-path`。  
> 多数据集时用 `--dataset sequential`（顺序）或 `--dataset mixed`（混合）+ 多个 `--dataset-path`。

`custom_multi_turn`（如 `conversation.jsonl`）每行是完整多轮消息数组：

```json
[{"role":"user","content":"..."},{"role":"assistant","content":"..."},{"role":"user","content":"..."}]
```

当前单轮 runner 会把整段对话作为 **一次** chat 请求发送（可选 `--max-turns` 截断）；逐轮 multi-turn runner 后续支持。

`line_by_line` 每行支持：

- 纯文本
- 消息列表：`[{"role":"user","content":"..."}, ...]`
- 完整请求体：`{"messages":[...], "tools":[...]}` 或 `{"prompt":"..."}`

### 输出 / 观测


| 参数                          | 说明                                                                         | 默认                |
| --------------------------- | -------------------------------------------------------------------------- | ----------------- |
| `--outputs-dir`             | 结果根目录                                                                      | `results`         |
| `--wandb`                   | 开启 W&B（复用 EvalScope visualizer；需 `pip install wandb evalscope`）            | `false`           |
| `--wandb-project`           | W&B project                                                                | `foretoken-bench` |
| `--wandb-entity`            | W&B entity（可选）                                                             | 空                 |
| `--wandb-run-name`          | run 名；列表 sweep 时兼作 W&B group 名（默认 `{model}_{timestamp}`）                  | 空                 |
| `--collect-engine-metrics`  | 采集 vLLM Engine `/metrics`                                                  | 开启                |
| `--engine-metrics-url`      | Prometheus 地址；默认由 `--url` 推导为 `/metrics`                                   | 空                 |
| `--engine-metrics-interval` | Engine `/metrics` 轮询间隔（秒）；旧名 `--gpu-metrics-interval` 仍可作为 bench-params 别名 | `1.0`             |


完整参数列表：`foretoken bench --help`。

---

## 示例

```bash
# 固定 prompt
foretoken bench --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B --prompt "Hello" --parallel 2 --number 4 --max-tokens 16

# JSONL（custom_multi_turn）
foretoken bench --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B --dataset custom_multi_turn \
  --dataset-path conversation.jsonl --max-turns 4 \
  --parallel 4 --number 20

# 并发 sweep + Pareto（X=tokens/s/user，Y=tokens/s/GPU）
foretoken bench --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B --parallel 1,2,4,8 --number 50 --gpu-count 1

# bench-params：对外部已启动服务扫多组客户端参数
foretoken bench --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B --dataset random \
  --tokenizer-path /path/to/tokenizer \
  --bench-params foretoken/benchmarks/examples/bench_params.json \
  --experiment-name demo_bench --num-runs 1

# serve-params × bench-params（外部已启动服务；serve 组合仅作元数据/目录名）
foretoken bench --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B --dataset random \
  --tokenizer-path /path/to/tokenizer \
  --serve-params foretoken/benchmarks/examples/serve_params.json \
  --bench-params foretoken/benchmarks/examples/bench_params.json \
  --experiment-name demo_serve_bench --dry-run
```

### `--serve-params` / `--bench-params`

与 [vLLM Parameter Sweeps](https://docs.vllm.ai/en/stable/benchmarking/sweeps/) 相同的 JSON 格式（list of dict 或 `{name: {params}}`）：


| 参数                  | 说明                                                                   |
| ------------------- | -------------------------------------------------------------------- |
| `--bench-params`    | JSON：覆盖客户端/压测参数                                                      |
| `--serve-params`    | JSON：服务端参数组合（与 bench 做笛卡尔积）；**仅写入元数据/目录名**，需外部服务已按该配置启动             |
| `--link-vars`       | `serve_key=bench_key,...`，过滤笛卡尔积                                     |
| `--num-runs`        | 每组重复次数（默认 1）                                                         |
| `--dry-run`         | 只打印计划                                                                |
| `--experiment-name` | 结果子目录名（默认时间戳）                                                        |


`bench-params` 常用键（含 vLLM 别名）：`parallel` / `max_concurrency`、`number` / `num_prompts`、`max_tokens`、`temperature`、`dataset`、`dataset_path`、`tokenizer_path`、`min_prompt_length`、`max_prompt_length` 等。

示例文件：`[examples/bench_params.json](./examples/bench_params.json)`、`[examples/serve_params.json](./examples/serve_params.json)`。

结果布局（**参数组合扫参**；与下方「列表 sweep」的 `artifacts/` 布局不同）：

```
results/<experiment_name>/
├── param_sweep_config.json
├── summary.csv
├── summary.json                     # schema: param_sweep_rows.v1（含 rows[]）
├── pareto.png / pareto_frontier.json    # 实验根目录，非 artifacts/
├── pareto/
│   ├── by_serve/<name>.png
│   ├── by_bench/<name>.png
│   └── by_combination/<comb>.png
└── SERVE-<name>-BENCH-<name>/
    ├── summary.json                 # schema: param_combo_rows.v1
    └── run=0/<timestamp>/...        # 内层单次/列表 run（见「结果目录」）
```

Pareto 轴固定为 tokens/s/user × tokens/s/GPU；`--gpu-count` 控制 GPU 分母。点数据在 `summary.json` 的 `pareto_frontier`。  
若同时开 `--wandb`：列表 sweep 为 **group runs**（每并发点一个 run，共享 group）；参数组合扫参仍按内层 `run=N/<timestamp>/` 分别上报。

> `conversation.jsonl` 为多轮对话（`custom_multi_turn`），单行可能很长；建议加 `--max-turns 2`，或改用 `--prompt` / `--dataset openqa`。

---

## 指标与结果

### 客户端（stream 开启时）


| 指标          | 含义                                             |
| ----------- | ---------------------------------------------- |
| **Latency** | 端到端请求延迟（用户侧）                                   |
| **TTFT**    | Time To First Token（用户侧）                      |
| **TPOT**    | `(latency - TTFT) / max(output_tokens - 1, 1)` |
| 吞吐          | `request/s`、`token/s`、**`token/s/user`**       |
| 成功率         | 成功请求占比                                         |

`token/s/user` = `token/s / max(parallel, 1)`（开环 `parallel=-1` 时分母为 1）；写入 `summary.json` / `metrics_table.json` / 终端 Summary，并上报 W&B 键 `Output Throughput per User (tok/s)`。


### Engine（`--collect-engine-metrics`，默认开）

压测期间轮询 vLLM Prometheus `/metrics`，汇总到 `summary.json` 的 `engine` 字段，并写出时序 CSV。


| 指标                                 | 含义                                     |
| ---------------------------------- | -------------------------------------- |
| `num_requests_running` / `waiting` | 运行中 / 等待中请求数                           |
| `kv_cache_usage_perc`              | KV cache 占用比例                          |
| `prefix_cache_hit_rate`            | 窗口内前缀缓存命中率（有 queries 时）                |
| `*_delta`                          | 窗口内 counter 增量（tokens / preemptions 等） |


### 结果目录

**单次压测 / 列表 sweep**（`--parallel` 等为列表时）：

```
results/<YYYYMMDD_HHMMSS>/
├── config.json                # parallel/number/rate 列表；resolved 为本次标量（开环时 parallel=-1）
├── raw.json                   # 仅单次：per-request
├── sweep_points.json          # 仅列表 sweep：各点聚合 metrics
├── summary.json               # schema: single_metrics.v1 | list_sweep.v1
├── metrics_table.json
├── wandb/                     # 仅 --wandb
└── artifacts/
    ├── engine_metrics.csv           # 单次压测时序
    ├── engine_metrics_pN.csv        # sweep 各点（N=parallel；开环为 -1）
    ├── sweep.csv
    └── pareto.png                   # tokens/s/user vs tokens/s/GPU
```

命名约定：CLI / config / 落盘指标统一用 `**parallel` / `number` / `rate**`。`concurrency` / `max_concurrency` / `num_prompts` 仅为 bench-params（及旧产物）**输入别名**，落盘不再双写。开环时指标点 `parallel=-1`。

参数组合扫参的根目录布局见上文「`--serve-params` / `--bench-params`」。

终端 Summary / Sweep 结果以 **Unicode 表格** 打印：

- Configuration / Request / System / Engine / Duration：两列表格
- **User-level**：Metric × mean/p50/p95/p99 矩阵（含 Tok/s/user）
- Sweep Result / Pareto Frontier：多列表格
- 列表 sweep 结束时对 **best throughput** 点再打一遍完整 Summary

开启 W&B：

```bash
pip install wandb evalscope
foretoken bench --url ... --model ... --wandb --wandb-project foretoken-bench
```

- 单次压测：一个 W&B run（过程中可能多次 `log`，结束再打最终点）；含 `Output Throughput per User (tok/s)`。
- 列表 sweep：每个 `(parallel, number, rate)` 点一个 W&B run，**同一 group**（`--wandb-run-name` 或默认 `{model}_{timestamp}`），便于在 W&B UI 里对比。
- 参数组合扫参：见上文（按内层 run 分别上报）。

示例：

```bash
# 并发扫参 + W&B
foretoken bench --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B --dataset random \
  --tokenizer-path /path/to/tokenizer \
  --parallel 1,2,4 --number 8 \
  --max-tokens 32 --gpu-count 2 --wandb

# multi_turn → 额外有 Avg Turns/Request 等
foretoken bench --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B --dataset custom_multi_turn \
  --dataset-path conversation.jsonl --max-turns 2 --number 10 --wandb

# 开环（须 --open-loop；仅 --rate 仍是闭环 + 泊松 pacing）
foretoken bench --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B --number 20 --rate 5 --open-loop --wandb
```

---

## 相关文档

- `[prd.md](./prd.md)` — 能力规划、指标三类约定、实现路线图
- `foretoken bench --help` — 参数权威列表

