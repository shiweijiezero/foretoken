# Parameter sweeps

English | [简体中文](sweep_zh.md) · [Common commands](../examples.md)

After [setup](../examples.md#setup), use the existing [parameter file](../../examples/sweep.jsonl) for repeatable comparisons. It sends 384 requests at each concurrency level (1, 2, 4), requesting 256 output tokens per request. The example below repeats every point three times.

Use a diagnostic deployment with fixed replicas and GPU allocation. When comparing inference settings, set `spec.engineArgs.enable-prefix-caching: false` for every variant so repeated inputs do not become a prefix-cache experiment. Record the model and tokenizer revisions, GPU and driver, runtime image digest, vLLM version and engine settings with the results.

Name the experiment once and deploy the baseline:

```bash
EXPERIMENT="quickstart-$(date -u +%Y%m%dT%H%M%SZ)"
VARIANT=baseline
foretoken deploy examples/quickstart --timeout 20m
```

Keep this deployment running and warm up before each repetition. Sixteen conversations are an initial budget; increase it equally across variants if timings have not stabilized:

```bash
foretoken bench examples/quickstart \
  --dataset random --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 --max-prompt-length 256 --random-seed 0 \
  --temperature 0 \
  --sweep benchmarks/examples/sweep.jsonl \
  --warmup-requests 16 --num-runs 3 --experiment-name "$EXPERIMENT-$VARIANT" \
  --wandb-group "$EXPERIMENT" --wandb-run-name "$VARIANT" \
  --output local,wandb
```

Each JSONL row defines a parameter group. Lists of `parallel`, `number`, or `rate` expand into points. Only one of `parallel` and `rate` may be a multi-value list in a row; a multi-value `number` list must match that axis's length.

Each row may change load, generation, or dataset settings, including output-length bounds. Service identity, credentials, trace source, and output destinations stay fixed. Sweeps cannot be combined with trace replay or multiple datasets. `--num-runs` repeats each point.

`--warmup-requests` defaults to zero and can also be set as `warmup_requests` in a sweep row. Warmup uses the same settings and starting data as measurement, finishes first, and must succeed. Its results are saved separately under each repetition's `warmup/` directory and excluded from measured metrics.

Each point has a result directory. `sweep_points.json` records all repetitions; `sweep_summary.json` and `sweep_summary.csv` report per-point mean, median, sample standard deviation and range. Missing values have an explicit sample count. Run-level p95 statistics are not pooled request percentiles. `pareto/PARETO.png` compares output token throughput per configured user with throughput per declared GPU when enough points are available. Choose a fresh `--experiment-name` for another experiment, or omit it to use an automatically created directory.

## Compare inference configurations

Reuse this procedure and parameter file for ordinary precision, quantized weights and speculative decoding. Configure them through the existing [inference parameters](../../../docs/inference-parameters.md). Compare checkpoints of the same model with a compatible tokenizer; do not attribute differences between unrelated models to quantization.

For each variant:

1. Change only the inference settings being evaluated. Keep hardware, replicas, context limit, unrelated engine settings, tokenizer, workload and request parameters fixed. For an AWQ weight comparison, use the same supported computation dtype in its ordinary-precision baseline.
2. Set a distinct `VARIANT` such as `awq` or `ngram`, keeping `EXPERIMENT` unchanged. Run `foretoken deploy examples/quickstart --timeout 20m` again, then repeat warmup and measurement. `bench` reuses existing services without applying YAML changes; a successful rollout is necessary before labeling a run with the new configuration.
3. Save the rendered deployment alongside each result after measurement:

   ```bash
   kubectl kustomize examples/quickstart > "results/$EXPERIMENT-$VARIANT/deployment.yaml"
   git rev-parse HEAD > "results/$EXPERIMENT-$VARIANT/commit.txt"
   ```

   Each repetition's `environment.json` also records client provenance and, for Kustomize sources, observed service/group settings, pod image IDs and node placement before and after the run. Record GPU model, driver and inference-engine versions and any local configuration changes separately. Desired YAML alone does not establish which runtime served the requests.

Use the existing W&B group to compare matching concurrency points, or read each experiment's `sweep_points.json`. Retain every repetition and failure. Check actual input/output lengths and successful request counts, then compare the median and range across repetitions for output throughput, TTFT, TPOT and end-to-end latency. A median of per-run p95 values is not a pooled p95. Use separate runs without profiling for performance comparisons.

Random inputs control length but do not establish speculative speedup on real tasks. Repeat with a fixed representative [conversation dataset](conversations.md), using identical rows and turn limits for every variant. When adapting the same sweep to real prompts, remove `min_output_length` and `max_output_length` from its rows (these require `--dataset random`) and choose a common `max_tokens` cap. If the experiment requires disjoint warmup data, leave automatic warmup disabled and use separate commands with different dataset offsets. Quantization also needs an independent output-quality evaluation; HTTP success is not a quality score.

For timing diagnosis, use the existing [benchmark profiling workflow](../../../observability/profiling.md) on a selected point and inspect it with `foretoken profile view`. After inspecting or exporting captures, delete the diagnostic service with `foretoken delete examples/quickstart`.

## Example output

Qwen3-0.6B on one A100 80GB PCIe GPU:

![Recorded sweep output](../imgs/sweep-cli.png)

W&B shows E2EL p95 in one-second completion windows; the Pareto plot compares whole-run throughput.

![E2EL p95 over elapsed time, in one-second completion windows](../imgs/sweep-wandb.png)

![Measured Pareto frontier](../imgs/sweep-pareto.png)
