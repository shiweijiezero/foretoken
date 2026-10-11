# StudyChat trace replay

English | [简体中文](studychat_zh.md) · [Performance examples](README.md)

After [setup](README.md#setup), replay a short populated window from the public trace:

```bash
foretoken perf examples/quickstart \
  --trace KrisQ/StudyChat --dataset KrisQ/StudyChat \
  --trace-start 18609050.546s --trace-duration 8min \
  --max-concurrency 16 --max-tokens 4096 \
  --output local,wandb,plot
```

`--trace` supplies arrival times; `--dataset` supplies content. Selecting the same source uses each record's own messages. The start offset is measured from the earliest timestamp in the dataset; gaps between records can be long. The command selects an eight-minute window, not a wait of 18 million seconds before replay. Replay starts at the first selected request and preserves subsequent arrival intervals; results retain offsets from the original window. In-flight requests run to completion, and concurrency waits are included in replay-delay metrics.

Each trace record is independent rather than a causal multi-turn conversation. Request count and timing come from the selected window, so omit `--num-prompts` and `--request-rate`, and positive `--max-turns`. Control in-flight requests with `--max-concurrency`.

## Example output

![Remote StudyChat replay by scheduled arrival time](../imgs/trace-studychat-wandb-dashboard.png)
