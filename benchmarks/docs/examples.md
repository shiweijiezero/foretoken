# Random dataset

```
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B \
  --dataset random \
  --tokenizer-path Qwen/Qwen3.6-27B \
  --min-prompt-length 128 --max-prompt-length 512 \
  --parallel 4 --number 20 --max-tokens 64 \
  --rate 5 \
  --wandb
```

![Random dataset benchmark output](imgs/random-dataset-benchmark-output.png)

![Random dataset W&B dashboard](imgs/random-dataset-wandb-dashboard.png)

# HuggingFace dataset

```
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B \
  --dataset weijiezz/foretoken-trace:conversation \
  --parallel 4 \
  --number 20 \
  --wandb
```

![Hugging Face dataset benchmark output](imgs/huggingface-dataset-benchmark-output.png)

![Hugging Face dataset W&B dashboard](imgs/huggingface-dataset-wandb-dashboard.png)

# Local dataset

```
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B \
  --dataset /home/wshiah/code/zhuting/foretoken/conversation.jsonl \
  --parallel 4 \
  --number 20 \
  --wandb
```

![Local dataset benchmark output](imgs/local-dataset-benchmark-output.png)

![Local dataset W&B dashboard](imgs/local-dataset-wandb-dashboard.png)

# Multi-dataset

```
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train,ianncity/GLM-5.2-Conversation:train \
  --parallel 4 \
  --number 20 \
  --wandb
```

![Multi-dataset benchmark output](imgs/multi-dataset-benchmark-output.png)

![Multi-dataset W&B comparison](imgs/multi-dataset-wandb-comparison.png)

# Load sweep (concurrency) + Pareto

```
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B \
  --dataset random \
  --tokenizer-path Qwen/Qwen3.6-27B \
  --min-prompt-length 128 --max-prompt-length 512 \
  --parallel 1,2,4,8 --number 20 --max-tokens 64 \
  --gpu-count 2 \
  --wandb
```

After all points finish, results include `pareto/PARETO.png`
(X = Tok/s/user, Y = Tok/s/GPU).

![Concurrency-sweep benchmark output](imgs/concurrency-sweep-benchmark-output.png)

![Concurrency-sweep W&B comparison](imgs/concurrency-sweep-wandb-comparison.png)


# Param sweep (bench-params)

Bench JSON overrides load / generation / dataset fields per combination.
Example: `examples/bench_params.json`.

```
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B \
  --dataset random \
  --tokenizer-path Qwen/Qwen3.6-27B \
  --min-prompt-length 128 --max-prompt-length 512 \
  --bench-params examples/bench_params.json \
  --gpu-count 2 \
  --wandb
```

![Param-sweep benchmark output](imgs/param-sweep-benchmark-output.png)

![Param-sweep W&B comparison](imgs/param-sweep-wandb-comparison.png)
