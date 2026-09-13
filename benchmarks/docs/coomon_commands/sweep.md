# Parameter sweeps

English | [简体中文](sweep_zh.md) · [Common commands](../examples.md)

After [setup](../examples.md#setup), run the maintained parameter file against one Kustomize deployment:

```bash
foretoken bench examples/quickstart \
  --dataset random --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 --max-prompt-length 512 \
  --sweep benchmarks/examples/sweep.jsonl \
  --experiment-name quickstart-sweep \
  --output local,wandb
```

The [parameter file](../../examples/sweep.jsonl) contains JSONL rows. List values for `parallel`, `number`, or `rate` expand into points. Only one of `parallel` and `rate` may be a multi-value list in a row; a multi-value `number` list must match that axis's length.

Each row may change load, generation, or dataset settings, including output-length bounds. Service identity, credentials, trace source, and output destinations stay fixed. Sweeps cannot be combined with trace replay or multiple datasets. `--num-runs` repeats each point.

Each point has a result directory. `sweep_points.json` records all results, and `pareto/PARETO.png` compares output token throughput per configured user with throughput per GPU when enough points are available. Choose a fresh `--experiment-name` for another experiment, or omit it to use an automatically created directory.
