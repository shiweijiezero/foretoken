# Parameter sweeps

English | [简体中文](sweep_zh.md) · [Common commands](../examples.md)

After [setup](../examples.md#setup), run the existing [parameter file](../../examples/sweep.jsonl) to compare concurrency levels:

```bash
foretoken bench examples/quickstart \
  --dataset random --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 --max-prompt-length 256 --random-seed 0 \
  --temperature 0 \
  --sweep benchmarks/examples/sweep.jsonl \
  --warmup-requests 16 --num-runs 3 \
  --output local,wandb
```

The file sends 384 requests at each concurrency level (1, 2, 4), requesting 256 output tokens per request. `--num-runs 3` repeats each point three times. `--warmup-requests 16` completes a separate warmup before every repetition; the default is zero.

Each JSONL row defines a parameter group. Lists of `parallel`, `number`, or `rate` expand into points. Only one of `parallel` and `rate` may be a multi-value list in a row; a multi-value `number` list must match that axis's length.

Rows may override load, generation and dataset settings, including `warmup_requests`. The deployment, credentials and output destinations stay fixed. Sweeps require a Kustomize deployment and cannot be combined with `--url`, trace replay or multiple datasets.

## Read results

The command prints its result directory under `results/`. `sweep_points.json` retains every repetition; `sweep_summary.json` and `sweep_summary.csv` report each point's mean, median, sample standard deviation and range. Start with successful request counts, then compare latency and throughput across repetitions. See [Experiment records](../../metrics.md#experiment-records) for units and missing-value semantics.

Each repetition has its own request records and a `warmup/` directory. Warmup results are excluded from measured metrics. `pareto/PARETO.png` compares output token throughput per configured user with throughput per declared GPU when enough points are available.

Use `--experiment-name baseline` to name an experiment, or omit it for an automatically created directory. Choose a fresh name for each experiment.

## Compare inference configurations

For precision, quantization or speculative-decoding comparisons, configure each variant through the existing [inference parameters](../../../docs/inference-parameters.md). Keep hardware, replicas, tokenizer, workload and unrelated engine settings fixed. Use corresponding checkpoints of the same model; an AWQ comparison should use the same supported computation dtype in its ordinary-precision baseline.

Set the same prefix-cache policy and warmup budget for all variants. To measure inference without repeated-prefix reuse, set `spec.engineArgs.enable-prefix-caching: false`. Automatic warmup uses the same starting rows and seed as measurement; use separate commands with different dataset offsets when disjoint data is required.

Deploy each configuration before running the sweep:

```bash
foretoken deploy examples/quickstart --timeout 20m
```

Repeat the benchmark command above with `--experiment-name baseline --wandb-group comparison`, then use distinct experiment names such as `awq` or `ngram` with the same group. After changing the model configuration, run `foretoken deploy` again: `bench` reuses existing services without applying YAML changes.

For a run named `baseline`, save the deployment configuration beside its results:

```bash
kubectl kustomize examples/quickstart > results/baseline/deployment.yaml
git rev-parse HEAD > results/baseline/commit.txt
```

Each repetition's `environment.json` records the client and observed serving settings. Also retain the server GPU, driver and inference-engine versions, resolved model/tokenizer revisions, and any local configuration changes needed to reproduce the comparison.

Use the W&B group or the local summaries to compare matching workload points, retaining failures as well as successful repetitions. Random inputs control length; use a fixed representative [conversation dataset](conversations.md) to assess real-task performance. For those datasets, remove `min_output_length` and `max_output_length` from the sweep rows and choose a common `max_tokens` cap. Quantization also needs output-quality evaluation.

Use separate [profiling runs](../../../observability/profiling.md) to diagnose timings. When finished with an explicitly deployed comparison service, run `foretoken delete examples/quickstart`.

## Example output

Qwen3-0.6B on one A100 80GB PCIe GPU:

![Recorded sweep output](../imgs/sweep-cli.png)

W&B shows E2EL p95 in one-second completion windows; the Pareto plot compares whole-run throughput.

![E2EL p95 over elapsed time, in one-second completion windows](../imgs/sweep-wandb.png)

![Measured Pareto frontier](../imgs/sweep-pareto.png)
