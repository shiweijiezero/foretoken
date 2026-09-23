# Arrival patterns and concurrency

English | [简体中文](arrival-rate_zh.md) · [Common commands](../examples.md)

After [setup](../examples.md#setup), send at an average of five requests per second while allowing up to sixteen concurrent requests:

```bash
foretoken perf examples/quickstart \
  --prompt Hello --request-rate 5 --max-concurrency 16 --num-prompts 100 \
  --output local,wandb
```

`--request-rate` controls the target request rate and `--max-concurrency` limits in-flight requests. The default `--arrival-pattern poisson` uses Poisson arrivals; use `constant` for fixed intervals or `gamma` with `--burstiness` for bursty arrivals; use `--trace` separately for timestamp replay. `--request-rate -1` sends as fast as possible, and `--max-concurrency -1` removes the concurrency cap. Defaults are no rate limit and one concurrent request. Generated arrivals, multi-turn conversations, and multiple datasets use the same request-rate, concurrency, warmup, and duration controls.

To remove the concurrency cap:

```bash
foretoken perf examples/quickstart \
  --prompt Hello --request-rate 5 --max-concurrency -1 --num-prompts 100 \
  --output local,wandb
```

With `--request-rate -1 --max-concurrency -1`, the entire request budget starts as fast as possible. Add `--duration SECONDS` to stop admissions at a wall-clock deadline; omit `--num-prompts` for a duration-bounded workload. Multi-turn data uses the same HTTP request budget and limits conversations in progress.

To use fixed or Gamma arrivals:

```bash
foretoken perf examples/quickstart \
  --prompt Hello --request-rate 5 --arrival-pattern constant \
  --max-concurrency 16 --num-prompts 100 --output local

foretoken perf examples/quickstart \
  --prompt Hello --request-rate 5 --arrival-pattern gamma \
  --burstiness 0.5 --max-concurrency 16 --num-prompts 100 --output local
```

## Example output

A short run with a lower arrival rate:

![CLI output](../imgs/arrival-rate-cli.png)

![W&B run](../imgs/arrival-rate-wandb.png)
