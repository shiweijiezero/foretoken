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

Use `--output local` for local files only, `--output local,quiet` to suppress the console summary, or `--output wandb` for upload only.
