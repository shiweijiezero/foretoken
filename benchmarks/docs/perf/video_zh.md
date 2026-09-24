# 视频生成评测

[English](video.md) | 简体中文 · [性能评测示例](README_zh.md)

在仓库根目录中，针对已运行的同步视频生成端点执行评测。开始前会检查同一服务的 `/health`；公开健康端点位于其他地址时，用 `--health-url` 指定。

## VideoArgusBench 数据集

| 数据集 selector | 服务端任务 |
| --- | --- |
| `VideoArgusBench/T2V` | `t2va` |
| `VideoArgusBench/TI2V` | `fl2va` |
| `VideoArgusBench/TS2V` | `ref2va` |
| `VideoArgusBench/TV2V` | `ref2va` |
| `VideoArgusBench/TSV2V` | `ref2va` |

## TI2V 评测

```bash
foretoken perf video \
  --url http://127.0.0.1:8091/v1/videos/sync \
  --dataset VideoArgusBench/TI2V \
  --num-prompts 10 \
  --width 1024 \
  --height 576 \
  --num-frames 124 \
  --fps 24 \
  --num-inference-steps 50 \
  --aspect-ratio 16:9 \
  --flow-shift 12 \
  --audio-flow-shift 3 \
  --seed 1 \
  --timeout 3600 \
  --max-concurrency 1 \
  --output local,wandb \
  --output-dir results/video/ti2v
```

评测会在终端打印汇总结果，在本地保存生成视频和运行元数据，并在 W&B 中记录逐请求指标。添加 `--warmup-requests N` 可在测量前完成前 N 条数据集请求；添加 `--duration SECONDS` 可在到达截止时间后停止新的请求准入，并等待已经准入的请求完成。使用 `--sweep benchmarks/examples/video-sweep.jsonl` 可比较视频生成参数和负载设置。通用输出设置见 [W&B 输出](wandb_zh.md)。

![TI2V 评测汇总结果](../imgs/video-ti2v-benchmark-summary.png)

![W&B 中的 TI2V 逐请求指标](../imgs/video-ti2v-wandb-requests.png)

![TI2V 本地评测产物](../imgs/video-ti2v-local-artifacts.png)

## TV2V 评测

```bash
foretoken perf video \
  --url http://127.0.0.1:8091/v1/videos/sync \
  --dataset VideoArgusBench/TV2V \
  --num-prompts 10 \
  --width 1024 \
  --height 576 \
  --num-frames 124 \
  --fps 24 \
  --num-inference-steps 50 \
  --aspect-ratio 16:9 \
  --flow-shift 12 \
  --audio-flow-shift 3 \
  --seed 1 \
  --timeout 3600 \
  --max-concurrency 1 \
  --output local,wandb \
  --output-dir results/video/tv2v
```

结果目录结构和 W&B 上报方式与 TI2V 相同：

![TV2V 评测汇总结果](../imgs/video-tv2v-benchmark-summary.png)

![W&B 中的 TV2V 逐请求指标](../imgs/video-tv2v-wandb-requests.png)

![TV2V 本地评测产物](../imgs/video-tv2v-local-artifacts.png)
