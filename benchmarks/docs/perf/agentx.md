<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Replay AgentX workloads

English | [简体中文](agentx_zh.md) · [Performance examples](README.md)

Use `foretoken perf --agentx` to measure a model service with the official AgentX session replay. The workload preserves long-context prefix reuse, sequential turns, parallel subagents, and tool waiting time. Concurrency counts active agent sessions.

## Run a measurement

Complete the [shared setup](../../README.md#get-started) and prepare a model service whose context window and memory support the selected corpus. The default corpus, `semianalysis_cc_traces_weka_062126`, includes contexts up to 1M tokens. Its `_256k` variant retains requests fitting within 256k input-plus-output tokens.

Replace `path/to/deployment` with that service's Kustomize directory. This example selects the 256k corpus:

```bash
foretoken perf path/to/deployment --agentx \
  --dataset semianalysis_cc_traces_weka_062126_256k \
  --max-concurrency 16 --output local,wandb,plot
```

Foretoken discovers the model, tokenizer, endpoint, Gateway routing headers, and declared GPU capacity from the deployment. The official scenario performs warmup before its default one-hour profiling window. Each sweep point and repetition has its own warmup and measurement.

The first invocation prepares AgentX Harness 1.0.12 in a separate cached Python 3.13 environment through uv. It also fetches the tokenizer and corpus when they are not cached. Subsequent runs reuse these downloads. Run `wandb login` before selecting W&B output, or use `--output local,plot` for local results and figures.

For an existing API, set `MODEL_SERVICE_URL` to its full Chat Completions endpoint and `MODEL_ID` to its served model name:

```bash
foretoken perf --agentx --url "$MODEL_SERVICE_URL" --model "$MODEL_ID" \
  --dataset semianalysis_cc_traces_weka_062126_256k \
  --max-concurrency 16 --output local,wandb,plot
```

Add `--api-key` when required. A model alias may need `--tokenizer-path` pointing to its Hugging Face tokenizer or a local tokenizer directory. For an existing Foretoken frontend URL, add `--extra-body '{"include_reasoning":true}'` so reasoning tokens remain visible to the timing client; deployment-path mode requests this automatically.

## Compare configurations

Pass several deployment paths, or several URLs with one shared model name or one name per URL, to measure methods in sequence. Use the [parameter-sweep format](sweep.md) to vary replay settings. For example, save this as `agentx-sweep.jsonl`:

```json
{"max_concurrency":[1,4,16]}
```

```bash
foretoken perf path/to/deployment --agentx \
  --dataset semianalysis_cc_traces_weka_062126_256k \
  --sweep agentx-sweep.jsonl --num-runs 3 \
  --output local,wandb,plot
```

AgentX uses trace-defined response lengths. Replay controls such as `--max-context-length`, `--random-seed`, and `--warmup-requests-per-lane` are listed by `foretoken perf --agentx --help`. Omitted replay controls use the official scenario's defaults. See the [official AgentX guide](https://github.com/SemiAnalysisAI/agentx-harness/blob/agentx-v1.0.12/docs/tutorials/agentx-mvp.md) for the corpus and methodology rules.

Repeat an interrupted sweep command with `--resume RESULT_DIR`, using its printed result directory. Completed measurements are reused; unfinished points restart from warmup. Keep the original result directories. For a completed run, `foretoken perf --resume RESULT_DIR` resumes result publication or figure export without repeating measurements.

## Read and plot results

The console reports observed requests, throughput, interactivity, and the native submission-validity outcome. Inspect these files under the printed result directory:

| File | Contents |
| --- | --- |
| `metrics.json` | Imported counts, rates, normalized metrics, and AgentX validity metadata |
| `agentx.log` | Native client execution and errors |
| `native/profile_export_aiperf.json` | Official profiling summary, configuration, and validity reasons |
| `native/profile_export.jsonl` | Native per-request metrics, including their warmup or profiling phase |

AgentX interactivity uses successful profiling requests' native token intervals, preferring full-response ITL when available. Select it separately from Foretoken's TPOT-based decode speed:

```bash
foretoken plot results/baseline results/candidate \
  --x-metric agentx_interactivity --x-stat p95 \
  --y-metric total_tokens_per_second_per_gpu \
  --frontier max-max --label-values --columns 2
```

For URL results without recorded GPU capacity, supply the actual count with `--gpu-count`. Revenue selection uses the same metric through `--interactivity-metric agentx_interactivity --interactivity-stat p95`. Definitions are in [performance metrics](../metrics.md#agentx-metrics); figure selection, pricing, and W&B recovery are in the [plotting guide](../plotting.md).

## Check a development trace

Use `--input-file` for a local trace in the [official Weka format](https://github.com/SemiAnalysisAI/agentx-harness/blob/agentx-v1.0.12/docs/tutorials/weka-trace.md). Replace both paths below with the deployment and trace prepared for the check:

```bash
foretoken perf path/to/deployment --agentx \
  --input-file path/to/weka-trace.json --unsafe-override \
  --max-concurrency 1 --duration 30s \
  --warmup-requests-per-lane 1 --agentic-warmup-grace-period 60s \
  --record-processors 1 --output local,plot
```

`--unsafe-override` permits exploratory scenario settings. The native report marks these runs as invalid for an official AgentX submission; read `submission_valid` and its reasons before comparing or publishing scores. AgentX synthetic replay measures serving performance, not model-answer quality.

If the native client stalls at `Processing Records`, retry with `--record-processors 1`. Ctrl+C interrupts the client and preserves available reports and logs. Early failures may have logs without a completed profiling summary.
