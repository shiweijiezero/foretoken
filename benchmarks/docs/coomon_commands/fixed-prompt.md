# Fixed prompts

English | [简体中文](fixed-prompt_zh.md) · [Common commands](../examples.md)

After [setup](../examples.md#setup), run from the repository root:

```bash
foretoken perf examples/quickstart \
  --prompt "Explain what a token is in one sentence." \
  --max-concurrency 4 --num-prompts 20 --max-tokens 64 \
  --output local,wandb
```

Each request uses the same prompt. Without `--prompt` or `--dataset`, a Kustomize benchmark uses `Hello`.

![Recorded CLI output](../imgs/fixed-prompt-cli.png)
