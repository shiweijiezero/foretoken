# Arrival rate and concurrency

English | [简体中文](arrival-rate_zh.md) · [Common commands](../examples.md)

After [setup](../examples.md#setup), send at an average of five requests per second while allowing up to sixteen concurrent requests:

```bash
foretoken bench examples/quickstart \
  --prompt Hello --rate 5 --parallel 16 --number 100 \
  --output local,wandb
```

`--rate` controls Poisson arrival rate and `--parallel` controls concurrency. Each accepts `-1` for no limit. Defaults are no rate limit and one concurrent request.

To remove the concurrency cap:

```bash
foretoken bench examples/quickstart \
  --prompt Hello --rate 5 --parallel -1 --number 100 \
  --output local,wandb
```

With `--rate -1 --parallel -1`, the entire request budget starts as fast as possible. Multi-turn data currently requires `--rate -1`; concurrency then counts conversations.

## Example output

A short run with a lower arrival rate:

![CLI output](../imgs/arrival-rate-cli.png)

![W&B run](../imgs/arrival-rate-wandb.png)
