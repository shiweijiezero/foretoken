# Video generation

English | [简体中文](video_zh.md) · [Common commands](../examples.md)

Run the benchmark from the repository root against an existing synchronous video-generation endpoint. The benchmark checks `/health` on the same server first; use `--health-url` if its public health endpoint is elsewhere.

## VideoArgusBench datasets

| Dataset selector | Endpoint task |
| --- | --- |
| `VideoArgusBench/T2V` | `t2va` |
| `VideoArgusBench/TI2V` | `fl2va` |
| `VideoArgusBench/TS2V` | `ref2va` |
| `VideoArgusBench/TV2V` | `ref2va` |
| `VideoArgusBench/TSV2V` | `ref2va` |

## TI2V benchmark

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

The benchmark prints an aggregate summary, saves the generated videos and run metadata locally, and records per-request metrics in W&B. Add `--warmup-requests N` to complete the first N rows before measurement, or `--duration SECONDS` to stop admitting new rows at a deadline and drain requests already admitted. Use `--sweep benchmarks/examples/video-sweep.jsonl` to compare video generation and load settings. See [W&B output](wandb.md) for shared output settings.

![TI2V aggregate benchmark summary](../imgs/video-ti2v-benchmark-summary.png)

![TI2V per-request metrics in W&B](../imgs/video-ti2v-wandb-requests.png)

![TI2V local benchmark artifacts](../imgs/video-ti2v-local-artifacts.png)

## TV2V benchmark

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

The result layout and W&B reporting are the same as for TI2V:

![TV2V aggregate benchmark summary](../imgs/video-tv2v-benchmark-summary.png)

![TV2V per-request metrics in W&B](../imgs/video-tv2v-wandb-requests.png)

![TV2V local benchmark artifacts](../imgs/video-tv2v-local-artifacts.png)
