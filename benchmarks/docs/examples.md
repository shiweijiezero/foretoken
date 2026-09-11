# Benchmark Recipes

English | [简体中文](examples_zh.md)

These recipes use one command, `foretoken bench`. For commands against an existing model service, set its full Chat Completions URL and model name first:

```bash
export MODEL_SERVICE_URL=http://127.0.0.1:8008/v1/chat/completions
export MODEL_ID=Qwen/Qwen3-0.6B
```

The service must support the OpenAI-compatible Chat Completions protocol. To benchmark a Foretoken deployment instead, replace `--url "$MODEL_SERVICE_URL" --model "$MODEL_ID"` with a Kustomize path such as `examples/quickstart`.

## Repeat a fixed prompt

```bash
foretoken bench \
  --url "$MODEL_SERVICE_URL" \
  --model "$MODEL_ID" \
  --prompt "Explain what a token is in one sentence." \
  --parallel 4 \
  --number 20 \
  --max-tokens 64 \
  --output local
```

A Kustomize benchmark uses `Hello` when neither `--prompt` nor `--dataset` is provided.

## Generate random prompts

Use random prompts to control input length without preparing a dataset. `--tokenizer-path` accepts a local tokenizer directory or a Hugging Face model ID.

```bash
foretoken bench \
  --url "$MODEL_SERVICE_URL" \
  --model "$MODEL_ID" \
  --dataset random \
  --tokenizer-path "$MODEL_ID" \
  --random-seed 0 \
  --min-prompt-length 128 \
  --max-prompt-length 512 \
  --prefix-length 64 \
  --parallel 4 \
  --number 20 \
  --max-tokens 64 \
  --output local
```

The minimum and maximum are inclusive lengths for the generated prompt content. `--prefix-length` adds the same prefix length to every request. Compare the measured input-token count with the requested range because the model service may apply its own chat template.

## Use a local JSONL dataset

Create a two-row dataset containing one single-turn and one multi-turn conversation:

```bash
cat > /tmp/foretoken-conversations.jsonl <<'JSONL'
{"messages":[{"role":"user","content":"Name a primary color."}]}
{"messages":[{"role":"system","content":"Answer briefly."},{"role":"user","content":"Name a planet."},{"role":"assistant","content":"Mars."},{"role":"user","content":"Name another one."},{"role":"assistant","content":"Venus."}]}
JSONL

foretoken bench \
  --url "$MODEL_SERVICE_URL" \
  --model "$MODEL_ID" \
  --dataset /tmp/foretoken-conversations.jsonl \
  --number 2 \
  --parallel 2 \
  --output local
```

The default `--max-turns -1` runs the complete conversation. In the second row, the follow-up question uses the model's first answer, which may differ from `Mars.` in the dataset.

Use `--max-turns 1` to run only the first user turn from each row:

```bash
foretoken bench \
  --url "$MODEL_SERVICE_URL" \
  --model "$MODEL_ID" \
  --dataset /tmp/foretoken-conversations.jsonl \
  --max-turns 1 \
  --number 2 \
  --output local
```

## Use a Hugging Face dataset

A dataset selector includes its split or configuration after the final colon:

```bash
foretoken bench \
  --url "$MODEL_SERVICE_URL" \
  --model "$MODEL_ID" \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train \
  --parallel 4 \
  --number 20 \
  --output local
```

A file inside a Hugging Face dataset repository can be selected directly:

```bash
foretoken bench \
  --url "$MODEL_SERVICE_URL" \
  --model "$MODEL_ID" \
  --dataset hf://datasets/ORG/REPOSITORY@REVISION/path/to/data.jsonl \
  --parallel 4 \
  --number 20 \
  --output local
```

Replace `ORG`, `REPOSITORY`, `REVISION`, and the file path with values from the dataset repository.

## Run ShareGPT conversations

ShareGPT rows use `conversations`, `from: human|gpt`, and `value`. Create a local example and run both user turns:

```bash
cat > /tmp/foretoken-sharegpt.jsonl <<'JSONL'
{"conversations":[{"from":"human","value":"Name a planet."},{"from":"gpt","value":"Mars."},{"from":"human","value":"Name another one."},{"from":"gpt","value":"Venus."}]}
JSONL

foretoken bench \
  --url "$MODEL_SERVICE_URL" \
  --model "$MODEL_ID" \
  --dataset /tmp/foretoken-sharegpt.jsonl \
  --max-turns 2 \
  --parallel 2 \
  --number 1 \
  --output local
```

The reference `gpt` text is replaced by the model's actual answer before the next `human` turn. Conversation datasets may contain system messages and structured message content, including image content arrays supported by the model service. They cannot contain top-level tool definitions, tool calls, or `tool` role messages.

## Combine multiple datasets

Comma-separated datasets run in order and produce one combined result. `--number` is divided as evenly as possible. Earlier datasets receive one extra row when necessary.

```bash
foretoken bench \
  --url "$MODEL_SERVICE_URL" \
  --model "$MODEL_ID" \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train,ianncity/GLM-5.2-Conversation:train \
  --parallel 4 \
  --number 20 \
  --output local
```

Random prompts cannot be mixed with another dataset. Multiple datasets cannot be combined with a parameter sweep.

## Set an arrival rate

Keep a concurrency cap while scheduling Poisson arrivals at five requests per second:

```bash
foretoken bench \
  --url "$MODEL_SERVICE_URL" \
  --model "$MODEL_ID" \
  --prompt "Hello" \
  --rate 5 \
  --parallel 16 \
  --number 100 \
  --output local
```

Remove the concurrency cap for an open-loop workload:

```bash
foretoken bench \
  --url "$MODEL_SERVICE_URL" \
  --model "$MODEL_ID" \
  --prompt "Hello" \
  --rate 5 \
  --open-loop \
  --number 100 \
  --output local
```

Actual multi-turn conversations do not support positive `--rate` or `--open-loop`.

## Replay a StudyChat trace

Each trace record is an independent request. Use the trace window and `--trace-max-concurrency` to control replay, not `--max-turns`, `--parallel`, `--number`, `--rate`, or `--open-loop`.

`--trace` supplies arrival timestamps; `--dataset` supplies request content. When both select StudyChat, the recorded request content is used directly.

```bash
foretoken bench \
  --url "$MODEL_SERVICE_URL" \
  --model "$MODEL_ID" \
  --trace KrisQ/StudyChat:train \
  --dataset KrisQ/StudyChat:train \
  --trace-start 600 \
  --trace-duration 300 \
  --trace-max-concurrency 32 \
  --output local
```

The replay starts 600 seconds into the trace and covers the next 300 seconds. Requests keep their recorded relative arrival times. If the concurrency limit delays a request, the delay appears in the replay metrics.

## Replay Mooncake prefix reuse

Mooncake records request lengths and shared prefixes, not the original text. Use it to generate synthetic prompts for a prefix-reuse experiment:

```bash
foretoken bench \
  --url "$MODEL_SERVICE_URL" \
  --model "$MODEL_ID" \
  --trace valeriol29/mooncake-traces:conversation \
  --trace-start 2620 \
  --trace-duration 30 \
  --dataset random \
  --tokenizer-path "$MODEL_ID" \
  --random-seed 0 \
  --trace-synthetic-prefix-reuse \
  --trace-max-concurrency 16 \
  --max-tokens 64 \
  --output local
```

Shared prefixes are generated from the trace's 512-token blocks. Server-side tokenization can change these boundaries, so verify cache hits with the model service's metrics.

## Sweep benchmark parameters

Parameter sweeps require a Foretoken Kustomize deployment and cannot be combined with trace replay or multiple datasets. The maintained file `benchmarks/examples/sweep.jsonl` defines two groups of concurrency points:

```jsonl
{"_benchmark_name": "n10", "parallel": [1, 2, 4, 8], "number": 10, "max_tokens": 64}
{"_benchmark_name": "n20", "parallel": [1, 2], "number": 20, "max_tokens": 128}
```

Run all points against one deployed or temporary model service:

```bash
foretoken bench examples/quickstart \
  --dataset random \
  --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 \
  --max-prompt-length 512 \
  --sweep benchmarks/examples/sweep.jsonl \
  --experiment-name quickstart-sweep \
  --output local
```

Each valid point is saved under the experiment directory. When at least two points succeed, `pareto/PARETO.png` compares generation tokens per second per configured user with generation tokens per second per GPU.

A JSONL row may change:

- load fields such as `parallel`, `number`, `rate`, and `open_loop`;
- generation fields such as `max_tokens`, `stream`, sampling parameters, and `extra_body`;
- dataset fields such as `dataset`, `max_turns`, prompt lengths, seed, and offset.

The model service, credentials, trace source, and result destinations remain fixed for the experiment. List values for `parallel`, `number`, or `rate` expand into points. Do not sweep both `parallel` and `rate` in the same row.

## Upload results to W&B

W&B upload is enabled by default. Log in once, then choose a project and run name if desired:

```bash
wandb login

foretoken bench examples/quickstart \
  --number 20 \
  --wandb-project foretoken-bench \
  --wandb-run-name quickstart \
  --output local,wandb
```

Use `--output local` when only local results are needed. See [Model Service Benchmarks](../README.md) for result locations and metric definitions.
