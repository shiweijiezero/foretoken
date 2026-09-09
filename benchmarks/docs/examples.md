# HTTP Performance Benchmark Examples

English | [简体中文](examples_zh.md)

## Dataset selectors

`--dataset` accepts local JSONL, Hugging Face datasets, and files in dataset repositories:

```text
/path/to/conversation.jsonl
org/dataset:train
hf://datasets/org/dataset@main/path/to/conversation.jsonl
```

Multiple dataset selectors may be comma-separated. A Hub file URI must include the `datasets` repository type.

## Random prompts

Random prompts require a tokenizer:

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --dataset random \
  --tokenizer-path Qwen/Qwen3-0.6B \
  --random-seed 0 \
  --min-prompt-length 128 --max-prompt-length 512 \
  --parallel 4 --number 20 --max-tokens 64 \
  --rate 5
```

Standard random loads use EvalScope 1.11.1's public `random` dataset plugin. `--random-seed` seeds its prompt-length, offset, and vocabulary sampling; `--min-prompt-length` and `--max-prompt-length` are inclusive bounds for the generated inner token sequence, while `--prefix-length` adds the shared prefix before decoding. The generated text is sent as one user message with `apply_chat_template=False`, so the server-reported input-token metrics remain the measured request size after decoding and chat-template processing.

## Hugging Face and local datasets

```bash
# Hugging Face dataset
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train \
  --parallel 4 \
  --number 20

# Local JSONL dataset
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --dataset /path/to/conversation.jsonl \
  --parallel 4 \
  --number 20
```

## Interactive multi-turn conversations

Every non-trace dataset row is executed as a conversation; a single user turn is the one-turn case. The default is `max_turns=-1` (use all turns), while `--max-turns N` truncates to the first N user turns. `--number` counts conversation rows and `--parallel` counts concurrently active conversations. Assistant messages in `messages` and verified ShareGPT rows are reference boundaries whose content is replaced by the model's answer. This is the dataset contract, not an inference of user intent from arbitrary history; tool definitions, tool calls, and `tool` role messages are rejected.

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --dataset kth8/multi-turn-conversation-50000x:train \
  --max-turns 4 \
  --parallel 2 --number 10
```

Local JSONL and Hugging Face rows may use an OpenAI-style `messages` field. A bare messages array is also accepted in local JSONL:

```jsonl
{"messages":[{"role":"system","content":"Answer briefly."},{"role":"user","content":"Name a primary color."},{"role":"assistant","content":"Red."},{"role":"user","content":"Name another one."},{"role":"assistant","content":"Blue."}]}
```

Verified ShareGPT rows using `conversations` with `from: human|gpt` and `value` are also accepted. Reference assistant messages only mark turn boundaries: EvalScope discards their content, appends the model's actual answer, and then sends the next user turn. A reference assistant message after the final user message therefore closes that final turn; it is not sent back to the model.

System messages and structured Chat Completions content such as image content arrays remain part of the message history. Tool definitions, tool calls, and `tool` role messages are rejected because this ordinary conversation runner does not execute a tool loop. Multi-turn currently cannot be combined with trace replay, open-loop mode, or a positive `--rate`.

Multiple multi-turn datasets still divide `--number` in selector order. The compatibility field `dataset_request_counts` therefore contains allocated conversation counts in this mode. Their top-level result exactly combines HTTP turn metrics, elapsed time, and attempted-conversation throughput; each dataset's conversation distributions remain under `conversation.per_dataset`. Parameter sweeps reuse the same multi-turn runner, and `max_turns` in a bench-params row also enables multi-turn mode for that point.

## Trace replay

`--trace` supplies arrival timestamps and `--dataset` supplies request content. The benchmark detects StudyChat and Mooncake trace formats. `--trace-start` and `--trace-duration` select `[first + start, first + start + duration)`; waiting for `--trace-max-concurrency` counts toward replay delay.

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --trace KrisQ/StudyChat \
  --dataset KrisQ/StudyChat \
  --trace-start 600 \
  --trace-duration 300 \
  --trace-max-concurrency 32
```

Replay a Mooncake trace with random payloads and shared synthetic prefixes:

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --trace valeriol29/mooncake-traces \
  --trace-start 2620 \
  --trace-duration 30 \
  --dataset random \
  --tokenizer-path Qwen/Qwen3-0.6B \
  --random-seed 0 \
  --trace-synthetic-prefix-reuse \
  --trace-max-concurrency 16 \
  --max-tokens 64
```

## Multiple datasets

Datasets run in order and their results are merged. `--number` is shared across the datasets and divided in selector order; earlier datasets receive one extra request when division is uneven.

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train,ianncity/GLM-5.2-Conversation:train \
  --parallel 4 \
  --number 20
```

## Parameter sweep

`--bench-params` accepts a JSONL file. Each line overrides request execution fields; list-valued `parallel`, `number`, and `rate` expand into separate points. A `rate` of `-1` sends requests as fast as possible.

`benchmarks/examples/bench_params.jsonl` contains a maintained example:

```jsonl
{"_benchmark_name": "n10", "parallel": [1, 2, 4, 8], "number": 10, "max_tokens": 64}
{"_benchmark_name": "n20", "parallel": [1, 2], "number": 20, "max_tokens": 128}
```

```bash
foretoken bench examples/quickstart \
  --dataset random \
  --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 --max-prompt-length 512 \
  --bench-params benchmarks/examples/bench_params.jsonl
```

Every valid point is saved. A sweep with at least two valid points also writes `pareto/PARETO.png`.
