# StudyChat trace replay

English | [简体中文](studychat_zh.md) · [Common commands](../examples.md)

After [setup](../examples.md#setup), replay a short populated window from the public trace:

```bash
foretoken perf examples/quickstart \
  --trace KrisQ/StudyChat --dataset KrisQ/StudyChat \
  --trace-start 18609050.546 --trace-duration 60 \
  --trace-max-concurrency 4 --max-tokens 32 \
  --output local,wandb
```

`--trace` supplies arrival times; `--dataset` supplies content. Selecting the same source uses each record's own messages. The start offset is measured from the earliest timestamp in the dataset; gaps between records can be long. The command selects a 60-second window, not a wait of 18 million seconds before replay. Concurrency waits are included in replay-delay metrics.

Each record is independent. Request count and timing come from the selected window, so omit `--num-prompts`, `--request-rate`, `--max-concurrency`, and positive `--max-turns`. Control in-flight requests with `--trace-max-concurrency`.

## Example output

![Remote StudyChat replay by scheduled arrival time](../imgs/trace-studychat-wandb-dashboard.png)
