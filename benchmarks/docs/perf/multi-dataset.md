# Multiple datasets

English | [简体中文](multi-dataset_zh.md) · [Performance examples](README.md)

After [setup](README.md#setup), separate dataset selectors with commas:

```bash
foretoken perf examples/quickstart \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train,ianncity/GLM-5.2-Conversation:train \
  --max-concurrency 4 --num-prompts 20 --output local,wandb
```

The datasets share the configured arrival rate, concurrency limit, and request budget. `--num-prompts` is divided evenly by default; `--dataset-weights 3,1` allocates three quarters to the first dataset. With `--duration` instead of `--num-prompts`, the weights control conversation sampling. Multi-turn conversations stop when their dataset's request budget is exhausted.

Local JSONL files or JSON conversation arrays can replace the remote datasets. Random inputs are used separately from other datasets.

A JSONL row with an integer token array, such as `{"prompt":[1,42,73],"output_length":32}`, is one pre-tokenized Completions request, not a conversation. The IDs must use the served model's tokenizer; no chat template is added. A string `prompt` retains its Chat Completions behavior. Recorded text answers supply per-turn output targets using the selected request model's tokenizer; see [conversation output lengths](conversations.md).

JSONL and Hugging Face rows can set `model`, `priority` (integer), `request_class` (benchmark label), and a positive integer `output_length`. For example, `{"prompt":"Hello","model":"Qwen/Qwen3-0.6B","request_class":"interactive","output_length":32}` sends that model and requests exactly 32 output tokens. Without `model`, the service selection applies. Rows with `model` can supply it instead of `--model` for URL or multi-model deployment workloads. `priority` is sent to the service and requires its priority-scheduling support; `request_class` labels requests for result breakdowns.

Results are grouped by dataset, model, and request class. Each group's throughput and goodput use the full experiment duration. See [parameter sweeps](sweep.md) to compare load settings or [SLO search](slo.md) to find a passing concurrency level.

## Example output

A short run over two local datasets:

![Combined CLI output](../imgs/multi-dataset-benchmark-output.png)

![Dataset curves compared in one W&B group](../imgs/multi-dataset-wandb.png)

Request-class p95 latency from a 3:1 interactive/batch workload against an existing endpoint:

![P95 end-to-end latency by request class](../imgs/mixed-workload-wandb.png)

The same workload requests 16 or 32 output tokens per dataset row; target and actual counts line up in send order:

![Per-request target and actual output tokens in W&B](../imgs/output-length-wandb.png)
