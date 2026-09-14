# StudyChat trace replay

English | [简体中文](studychat_zh.md) · [Common commands](../examples.md)

After [setup](../examples.md#setup), replay recorded request timing and content:

```bash
foretoken bench examples/quickstart \
  --trace KrisQ/StudyChat --dataset KrisQ/StudyChat \
  --trace-start 600 --trace-duration 300 \
  --trace-max-concurrency 32 --output local,wandb
```

`--trace` supplies arrival times; `--dataset` supplies content. Selecting the same source uses each record's own messages. The window begins 600 seconds into the trace and covers the next 300 seconds. Concurrency waits are included in replay-delay metrics.

Each record is independent. Request count and timing come from the selected window, so omit `--number`, `--rate`, `--parallel`, and positive `--max-turns`. Control in-flight requests with `--trace-max-concurrency`.

## Example output

A short local trace in StudyChat format, not the full remote dataset:

![CLI output](../imgs/trace-studychat-benchmark-output.png)

![W&B run](../imgs/trace-studychat-wandb-dashboard.png)
