# HTTP 性能评测

[English](README.md) | 简体中文

使用 `foretoken bench` 测量 Foretoken 部署或现有 OpenAI 兼容端点的延迟和吞吐量。

## 开始前

从仓库根目录使用 Python 3.10 或更高版本运行评测命令：

```bash
pip install 'foretoken[bench]'

# 如果使用源码安装：
# pip install -e .
# pip install -e '.[bench]'
```

评测 Foretoken 部署时，先安装平台，再评测 Kustomize 配置：

```bash
foretoken install
foretoken bench examples/quickstart
```

快速开始服务已运行时，命令会直接复用；否则会部署渲染后的资源，并在评测结束后只删除本次创建的资源。

评测现有端点时，不需要 Foretoken 或 Kubernetes 平台：

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --prompt "你好" \
  --parallel 2 \
  --number 20
```

## 结果与输出

不指定 `--output` 时，评测会打印汇总、在 `results/` 下保存本地产物，并尝试上传 W&B。W&B 不可用时，本地结果仍会保留。

标准请求负载使用 EvalScope 负责负载调度、HTTP 执行以及时延和 token 指标，同时保留本地或 Hub 数据中的完整请求体。只有 `--max-turns` 会启用多轮模式；使用 `--max-turns -1` 执行数据集定义的完整对话。此时每行数据表示一段交互式对话，每轮模型的真实回答会在发送下一轮用户消息前加入上下文。对应本地目录会同时保存 Foretoken 的 `config.json`、`metrics.json`，以及 EvalScope 的 `benchmark_args.json`、`benchmark_summary.json`、`benchmark_percentile.json`、`benchmark_data.db` 和 `benchmark.log`；多轮运行还会保存 `trace_summary.json`、`workload_throughput.json` 和 `workload_timeline.json`。轨迹回放会写入额外包含回放延迟字段的 `raw_output.json`。

标准负载的逐请求产物由原来的 `raw_output.json` 迁移为 `benchmark_data.db`；请求失败详情写入 `benchmark.log`。参数扫描和 W&B 继续消费字段稳定的 `metrics.json`。结果中的 `mode` 也改用职责名称：`run_benchmark` 改为 `standard_load`，`sweep` 改为 `parameter_sweep`；多数据集结果中的 `dataset_numbers` 改为 `dataset_request_counts`。多轮结果新增 `multi_turn: true` 和 `conversation` 对象；原有 `request_num`、`success_num`、时延和吞吐量字段继续表示逐轮 HTTP 请求。组合多个多轮数据集时，顶层会精确聚合逐轮指标与尝试对话吞吐量；由于 EvalScope 1.11.1 的 SQLite 请求记录不保存对话 ID，对话分位数保留在 `conversation.per_dataset`，不会将各数据集分位数平均成伪造的全局值。

`--output` 会替换默认输出选项：

| 目标 | `--output` 值 |
| --- | --- |
| 默认控制台、本地产物和 W&B | 不传 `--output` |
| 仅保存本地产物 | `local` |
| 保存本地产物但不输出控制台 | `local,quiet` |
| 保存本地产物并上传 W&B，但不输出控制台 | `local,wandb,quiet` |
| 仅上传 W&B | `wandb` |

如需关闭控制台输出但保留结果，请将 `quiet` 与 `local`、`wandb` 或两者组合。使用 `--output-dir PATH` 修改本地产物目录。

## 指标

汇总结果包括请求延迟、首个 token 时延（TTFT）、每输出 token 时延（TPOT）、失败率、输入/输出 token 数和输出吞吐量。多轮模式下，这些请求字段统计实际发送的 HTTP 轮次；`number` 表示配置的对话数，`parallel` 表示并发对话数，`conversation` 则包含尝试对话吞吐量，以及 EvalScope 提供的对话时延、首轮 TTFT、最终回答首 token 时间、解码吞吐量和缓存指标。任一轮失败都会终止该对话，因此不能把逐轮成功数解释为成功对话数。

参数扫描中的 `token/s/user` 表示输出吞吐量除以配置的 closed-loop `--parallel` 值，不表示真实用户数或活跃会话数。多轮参数扫描中，同一个分母明确表示并发对话数，控制台和 Pareto 图会使用对应名称。open-loop（`--rate`）的分母固定为一，因此它等于总输出吞吐量。`token/s/GPU` 表示输出吞吐量除以该负载点配置的 GPU 数。

扫描会保存每个有效负载点；只有至少有两个有效负载点时，才会生成 `pareto/PARETO.png`。

## 下一步

数据集、随机提示词、轨迹回放、前缀复用、多数据集和参数扫描的配方见[评测示例](docs/examples_zh.md)。命令参数和结果格式可通过 `foretoken bench --help` 及本地产物查看。
