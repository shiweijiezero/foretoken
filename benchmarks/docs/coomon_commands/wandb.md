# W&B output

English | [简体中文](wandb_zh.md) · [Common commands](../examples.md)

After [setup](../examples.md#setup), choose the project, group, and run name:

```bash
wandb login

foretoken bench examples/quickstart \
  --number 20 --output local,wandb \
  --wandb-project foretoken-bench \
  --wandb-group qwen-comparison \
  --wandb-run-name quickstart
```

`--wandb-entity` selects the account or team. Group and run name are independent. Sweeps and multi-dataset runs generate a group if none is supplied; child labels are appended to their run names. Single runs are ungrouped by default.

The W&B page shows final aggregate metrics, P50/P95/P99 percentile metrics, and time-based, cumulative, and per-request series. Kustomize runs also show replica-count changes. Open a group's Workspace to compare runs; Summary retains the final values. See [Result metrics](../../metrics.md#curves) for window definitions.

![Per-request timings and token counts](../imgs/request-order-wandb.png)

Use `--output local` for local files only, `--output local,quiet` to suppress the console summary, or `--output wandb` for upload only. When W&B is explicitly selected, an initialization, publication, or finalization failure makes the command fail and preserves any artifacts already produced.
