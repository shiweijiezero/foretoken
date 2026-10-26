# Qwen3.5-35B-A3B Optimization on MetaX

English | [简体中文](qwen3.5-35b-a3b-muxi_zh.md)

```text
Models: Qwen3.5-35B-A3B
Starting configuration: examples/rsi/qwen3.5-35b-a3b; two-way tensor parallelism (TP2), four-token multi-token prediction (MTP), a 36,864-token context, and no KV cache offload to host memory; adjustable during optimization
Optimization objectives: increase output throughput (tokens/s) and reduce time per output token (TPOT) at concurrency 1 and 16
Precision: fixed BF16
Deployment layout: agent-selected within the final single-node GPU budget, including parallelism and any stage separation
Final deployment budget: 2 MetaX GPUs on one node; at most 32 CPU cores and 250G host memory in total for the model and frontend workloads
Experiment sizing: service candidates fit the final budget; focused kernel, compilation, or correctness experiments may use fewer resources, including one GPU
Available resources: at most 6 MetaX GPUs in total for experiments; schedule CPU, memory, and storage within actual host capacity
Advisor count: 2
Worker count: 2
Experimenter count: 2
Observer count: 1
Observer check interval: 1h
Maximum parallel experiments: at most 3 two-GPU service candidates; smaller focused experiments share the six-GPU budget
Parallel scheduling: reserve capacity for platform services, builds, and benchmark clients, then schedule experiments by their actual resource requests and record the achieved parallelism
Evaluation workloads: measure concurrency 1 and 16 separately per service using random-length workloads and StudyChat conversations; see the full commands under "Evaluation commands" below
Accuracy evaluation: run foretoken eval as needed for changes that may affect accuracy, at most once per 20 completed performance iterations
Allowed changes: implementation and configuration at any layer, within the precision and resource constraints above
Measurement budget per iteration: default to 10 minutes of perf/eval measurement to keep iterations fast; adjust the measurement budget and request count to the actual workload, recording the reason and time spent
Work duration: a total budget of 24 hours from the start of task execution
Expected outcome / completion criteria: keep increasing throughput and reducing TPOT without fixed metric thresholds.
  Continue research, implementation and measurement, covering both parameter tuning and source changes. Finish when the research questions are answered and further work offers little value, when time runs out, or when the user asks to stop.
  Explain the evidence for stopping. Distinguish approaches tested without benefit, implementations that could not run, and ideas not yet implemented or tested.
Deliverables (<iteration> is the approach name passed to --iteration):
  - Iteration notes: record motivation, code and configuration changes, complete commands, results and next steps in results/qwen3.5-35b-a3b-muxi/iterations/<iteration>/notes/iteration.md.
  - Performance comparisons: in the notes, list output throughput (tokens/s) and mean TPOT (ms/token) at concurrency 1 and 16 for each workload. Compare results with the initial approach and previous iteration, explaining percentage changes and their causes.
  - Evidence: command artifacts are saved under runs/ in the same iteration directory; link raw results, plots and W&B from the notes.
  - Execution efficiency: report the time-weighted mean number of active subagents and each node's mean GPU utilization over the full task. State the interval, resource scope, sampling coverage and reasons for low utilization, with raw records.
  - User-facing report: continuously update rsi-playbooks/tasks/qwen3.5-35b-a3b-muxi/<experiment>/report.md with per-workload comparisons, changes from baseline, the retained approach and its code/configuration, conclusions and execution efficiency. Link iteration notes and W&B; place key plots in figures/ beside the report and use relative links.
```

Follow the [RSI skill](../../.agents/skills/rsi/SKILL.md) for execution and resource scheduling. Keep evidence using the [experiment records guide](../experiment-records.md), and organize the report with the [report template](../templates/report-template.md).

## Starting deployment

Use an existing MetaX source installation, or prepare one through the [MetaX platform guide](../../docs/deployment/metax-platform.md#install-from-source). Reuse compatible images and [shared model files](../../docs/features/model-storage.md). Deploy the [two-GPU example](../../examples/rsi/qwen3.5-35b-a3b/) from the candidate checkout:

```bash
foretoken deploy examples/rsi/qwen3.5-35b-a3b --name qwen-rsi-muxi -e . --timeout 60m
```

The command returns when the model and frontend are ready. Together they request 2 GPUs, 9 CPU cores and 100G memory, with combined CPU and memory limits of 26 cores and 196G.

## Evaluation commands

Run from the repository root and authenticate with `wandb login` before using W&B. Results go to `results/qwen3.5-35b-a3b-muxi/`. Use `baseline` for the initial approach, a new name for each new approach, and the same name for repeat measurements. To compare another candidate, replace its deployment directory, `--name` and `--iteration` together.

Each performance loop measures concurrency 1 and 16 in sequence, with the same inputs, 80 measured requests and four warmup requests per group. Run StudyChat for the final retained approach and as needed during intermediate iterations. GSM8K sends up to 64 concurrent requests, which the engine schedules within its sequence limit. See [benchmark examples](../../benchmarks/docs/recipes.md) for other workloads.

```bash
# Every performance iteration: random lengths at concurrency 1 and 16.
for concurrency in 1 16; do
  foretoken perf examples/rsi/qwen3.5-35b-a3b --name qwen-rsi-muxi --dataset random \
    --min-prompt-length 128 --max-prompt-length 4096 \
    --min-output-length 128 --max-output-length 512 \
    --max-concurrency "$concurrency" --num-prompts 80 --warmup-requests 4 \
    --random-seed 0 --temperature 0 --output experiment,raw,plot,wandb \
    --output-dir results/qwen3.5-35b-a3b-muxi --iteration baseline
done

# Required for the final retained approach: StudyChat at concurrency 1 and 16.
for concurrency in 1 16; do
  foretoken perf examples/rsi/qwen3.5-35b-a3b --name qwen-rsi-muxi \
    --dataset hf://datasets/KrisQ/StudyChat/data.jsonl \
    --max-concurrency "$concurrency" --num-prompts 80 --max-tokens 2048 \
    --warmup-requests 4 --temperature 0 --output experiment,raw,plot,wandb \
    --output-dir results/qwen3.5-35b-a3b-muxi --iteration baseline
done

# Accuracy evaluation as needed, with up to 64 concurrent requests.
foretoken eval examples/rsi/qwen3.5-35b-a3b --name qwen-rsi-muxi \
  --evaluator lm-eval --tasks gsm8k --num_fewshot 0 --limit 20 \
  --model_args num_concurrent=64 --output experiment,plot,wandb \
  --output-dir results/qwen3.5-35b-a3b-muxi --iteration baseline
```
