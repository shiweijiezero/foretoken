# Parameter sweeps

English | [简体中文](sweep_zh.md) · [Common commands](../examples.md)

After [setup](../examples.md#setup), compare concurrency levels with the existing [parameter file](../../examples/sweep.jsonl):

```bash
foretoken bench examples/quickstart \
  --dataset random --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 --max-prompt-length 256 --random-seed 0 \
  --temperature 0 \
  --sweep benchmarks/examples/sweep.jsonl \
  --warmup-requests 16 --num-runs 3 \
  --output local,wandb
```

This runs 384 requests at concurrency 1, 2 and 4, requesting 256 output tokens each. Every point is repeated three times, with 16 warmup conversations before each repetition. Pass a deployment configuration directory such as `examples/quickstart`; sweeps do not support `--url`, trace replay or multiple datasets.

Each JSONL row defines load, generation or dataset settings. Lists of `parallel`, `number` or `rate` expand into points. Only one of `parallel` and `rate` may have multiple values; a multi-value `number` must match that axis's length. Rows may also override `warmup_requests`.

## Read results

Open `sweep_summary.csv` in the printed result directory to compare repetitions. Individual results remain in each run's directory; [Result metrics](../../metrics.md#experiment-records) explains the saved files, statistics and units. Use `--experiment-name` to choose a fresh directory name, or omit it for an automatic name.

## Compare inference configurations

Change the model's [inference parameters](../../../docs/inference-parameters.md) and apply each configuration before repeating the same sweep:

```bash
foretoken deploy examples/quickstart --timeout 20m
```

`bench` reuses existing services without applying YAML changes. Keep the workload, hardware and cache policy consistent; append `--experiment-name baseline --wandb-group comparison` to the benchmark command, changing the experiment name for each variant. When finished, remove the explicitly deployed service with `foretoken delete examples/quickstart`.

## Example output

Qwen3-0.6B on one A100 80GB PCIe GPU:

![Recorded sweep output](../imgs/sweep-cli.png)

W&B shows E2EL p95 in one-second completion windows; the Pareto plot compares whole-run output tok/s per user and per declared GPU. Points without either denominator are omitted.

![E2EL p95 over elapsed time, in one-second completion windows](../imgs/sweep-wandb.png)
