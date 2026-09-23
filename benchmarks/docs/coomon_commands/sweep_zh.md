# 参数扫描

[English](sweep.md) | 简体中文 · [常用命令](../examples_zh.md)

完成[准备步骤](../examples_zh.md#准备)后，使用现有[参数文件](../../examples/sweep.jsonl)比较不同并发下的表现：

```bash
foretoken bench examples/quickstart \
  --dataset random --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 --max-prompt-length 256 --random-seed 0 \
  --temperature 0 \
  --sweep benchmarks/examples/sweep.jsonl \
  --warmup-requests 16 --num-runs 3 \
  --output local,wandb
```

该示例在 1、2、4 并发下各发送 384 个请求，每个请求要求输出 256 个 token。每个参数点重复三次，每次重复前预热 16 段对话。参数扫描需传入部署配置目录，例如 `examples/quickstart`；多轮和多数据集会使用与独立评测相同的全局调度器。

每行 JSONL 定义负载、生成或数据集设置。`max_concurrency`、`num_prompts`、`request_rate`、`arrival_pattern`、`burstiness`、`duration`、`warmup_requests`、`conversation_history` 等规范字段的列表会展开成笛卡尔组合。扫描行可以包含 SLO 条件，每个点分别执行自己的 SLO 搜索，也可以使用时间戳轨迹回放。新配置使用 `max_concurrency`，不再使用 `parallel`。

视频生成也复用同一套扫描生命周期和结果目录结构，但只展开视频自己的字段，不伪造 HTTP 指标：

```bash
foretoken bench video \
  --url http://127.0.0.1:8091/v1/videos/sync \
  --dataset VideoArgusBench/TI2V \
  --sweep benchmarks/examples/video-sweep.jsonl \
  --num-runs 2 --output local,wandb
```

视频扫描点可以改变 `width`、`height`、`num_frames`、`fps`、`num_inference_steps`、`aspect_ratio`、`flow_shift`、`audio_flow_shift`、`seed`、`max_concurrency`、`duration` 和 `warmup_requests`。每个点保留视频请求产物和视频指标，不增加 HTTP 延迟/吞吐字段或 HTTP Pareto 图。

## 查看结果

打开命令打印的结果目录，通过 `sweep_summary.csv` 比较重复运行。每次运行的明细保存在各自目录；文件内容、统计含义和单位见[结果指标](../../metrics_zh.md#实验记录)。可用 `--experiment-name` 指定一个未使用的目录名，省略时自动命名。

## 对比推理配置

修改模型的[推理参数](../../../docs/inference-parameters_zh.md)，先应用配置，再重复同一组扫描：

```bash
foretoken deploy examples/quickstart --timeout 20m
```

`bench` 复用已有服务时不会应用 YAML 修改。各方案保持负载、硬件和缓存策略一致，在评测命令后添加 `--experiment-name baseline --wandb-group comparison`，每种方案使用不同实验名。完成后，执行 `foretoken delete examples/quickstart` 删除显式部署的服务。

## 输出示例

Qwen3-0.6B 的 NVIDIA GPU 测量示例：

![扫描命令的实际输出](../imgs/sweep-cli.png)

W&B 按一秒完成窗口展示 E2EL p95；帕累托图比较整组运行中每用户、每张声明 GPU 的输出 tok/s。缺少任一分母的点不会进入图中。
