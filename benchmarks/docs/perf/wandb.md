# W&B output

English | [简体中文](wandb_zh.md) · [Performance examples](README.md)

After [setup](README.md#setup), choose the project, group, and run name:

```bash
foretoken perf examples/quickstart/qwen3-0.6b \
  --num-prompts 20 --output local,wandb \
  --wandb-project foretoken-bench \
  --wandb-group qwen-comparison \
  --wandb-run-name quickstart
```

Open the run link printed by the command; it also appears in quiet mode. Use `--wandb-entity` to select an account or team. Set the same `--wandb-group` for runs you want to compare in a group's Workspace. Sweeps and multi-dataset runs generate a group when none is supplied; single runs are ungrouped by default.

## Follow progress and compare results

Use request-order curves to inspect individual requests, cumulative curves to follow overall progress, and one-second completion windows to compare throughput and P50/P95/P99 latency over time. These views update during HTTP measurement; windows may wait for earlier unfinished requests. Final aggregate metrics appear when measurement finishes and remain in Summary.

![Per-request timings and token counts](../imgs/request-order-wandb.png)

Mixed workloads include breakdowns by dataset, model, and request class. Kustomize runs add replica and GPU-allocation observations; services with Prometheus can also report resource usage and speculative decoding. See [Performance metrics](../metrics.md) for definitions, units, and observation coverage.

## Download records

Open the run's Artifacts to download configuration, metrics, request records, and logs. For sweeps and comparisons, use the parent run's log to inspect shared service preparation and cleanup. File contents and output choices are listed in [saved results](../../README.md#read-and-save-results).

Warmup is excluded from performance metrics and panels. Its records remain in the `benchmark-warmup` artifact. Adding `plot` to `--output local,wandb` saves exported figures locally and in the `benchmark-plots` artifact.

## Resume an upload

Set `WANDB_MODE=offline` when running a benchmark to record locally without attempting uploads. To upload later, or retry a failed background upload, pass the result directory printed by the command:

```bash
foretoken plot RESULT_DIR --sync_wandb
```

This redraws local figures and synchronizes saved metrics, tables, media and logs to the original W&B runs without sending model requests. Interrupted runs can upload their available records and diagnostics. Figure export errors are reported, but do not prevent result synchronization; the command exits unsuccessfully if either stage fails.

Individual-run figures are uploaded with that run, including when using `--output-dir`. Cross-run comparison figures remain local. Repeating the command refreshes per-run figures without replaying published measurement history.

To upload from another machine, install Foretoken, run `wandb login`, and copy the complete result tree, including hidden files. Its absolute location may change. If a resumed experiment references earlier task results, copy those directories too, preserving their relative layout. For a self-hosted W&B server, set `WANDB_BASE_URL` to the original server.

Without `--sync_wandb`, `foretoken plot` only exports local files. Use `perf --resume` to continue unfinished measurements. Older recordings with missing external W&B files still require those files to be restored.
