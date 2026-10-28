# Qwen3.5-35B-A3B Optimization on MetaX

English | [简体中文](qwen3.5-35b-a3b-muxi_zh.md)

```text
Target model: Qwen3.5-35B-A3B [Fixed]
Starting configuration: examples/rsi/qwen3.5-35b-a3b; two-way tensor parallelism (TP2), four-token multi-token prediction (MTP), a 36,864-token context, and no KV cache offload to host memory; adjustable during optimization
Optimization objectives: increase output throughput (tokens/s) and reduce time per output token (TPOT) at concurrency 1 and 16 [Fixed]
Precision: fixed BF16 [Fixed]
Deployment layout: agent-selected within the final single-node GPU budget, including parallelism and any stage separation
Final deployment budget: at most 2 MetaX GPUs on one node; at most 32 CPU cores and 250G host memory in total for all target model, auxiliary model and frontend workloads [Fixed]
Experiment sizing: service candidates fit the final budget; focused kernel, compilation, or correctness experiments may use fewer resources, including one GPU
Available resources: at most 6 MetaX GPUs in total for experiments; schedule CPU, memory, and storage within actual host capacity [Fixed]
Explorer count: 2 [Fixed]
Worker count: 4 [Fixed]
Experimenter count: 3 [Fixed]
Evaluator count: 1 [Fixed]
Evaluator check interval: 1h [Fixed]
Codex models: Explorer = gpt-6-astra/high; Worker, Experimenter and Evaluator = gpt-6.1-sol/high [Fixed]
Claude models: Explorer = Fable/Opus/high; Worker, Experimenter and Evaluator = sonnet/high [Fixed]
Maximum parallel experiments: at most 3 two-GPU service candidates; smaller focused experiments share the six-GPU budget
Parallel scheduling: reserve capacity for platform services, builds, and benchmark clients, then schedule experiments by their actual resource requests and record the achieved parallelism
Iteration workloads: random only by default, at concurrency 1 and 16; analyze each point as it completes
Extended validation: StudyChat for multi-turn hypotheses and the final retained approach, reusing existing matched controls; quality evaluation as needed
Accuracy evaluation: run foretoken eval when an output-affecting change needs new quality evidence, prioritizing candidates considered for retention.
Allowed changes: implementation and configuration at any layer within fixed requirements, including algorithms, auxiliary models and their weights, composition and execution; develop new mechanisms or use community work
Measurement budget per iteration: allow 10 minutes by default and use enough samples to answer the question; analyze once they finish. Adjust the budget and request count to the workload, recording the reason and time spent
Work duration: a total budget of 24 hours from the start of task execution [Fixed]
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

Use random workloads for routine iterations. Run StudyChat when a multi-turn question or the final retained approach requires it. Deliver each completed concurrency point so the Worker can analyze it while the Experimenter continues measuring. Reuse matched results while they still apply. See [benchmark examples](../../benchmarks/docs/recipes.md) for other workloads.

### Routine iterations: random

Use 80 measured requests and four warmup requests per concurrency point, with the same inputs. Run with `concurrency=1`, deliver that result, then repeat with `concurrency=16`.

```bash
concurrency=1
foretoken perf examples/rsi/qwen3.5-35b-a3b --name qwen-rsi-muxi --dataset random \
  --min-prompt-length 128 --max-prompt-length 4096 \
  --min-output-length 128 --max-output-length 512 \
  --max-concurrency "$concurrency" --num-prompts 80 --warmup-requests 4 \
  --random-seed 0 --temperature 0 --output experiment,raw,wandb \
  --output-dir results/qwen3.5-35b-a3b-muxi --iteration baseline

```

### Multi-turn and final validation: StudyChat

Use 200 measured requests and four warmup requests per concurrency point, counting individual turns across conversations. Compare the final retained approach with its matched baseline, reusing results that still apply.

```bash
for concurrency in 1 16; do
  foretoken perf examples/rsi/qwen3.5-35b-a3b --name qwen-rsi-muxi \
    --dataset hf://datasets/KrisQ/StudyChat/data.jsonl \
    --max-concurrency "$concurrency" --num-prompts 200 --max-tokens 2048 \
    --warmup-requests 4 --temperature 0 --output experiment,raw,plot,wandb \
    --output-dir results/qwen3.5-35b-a3b-muxi --iteration baseline
done
```

### Quality evaluation as needed

GSM8K uses up to 64 concurrent requests. Its output budget includes reasoning and is capped by Foretoken at the deployed context limit minus the rendered input length: `36864 - input_tokens` for the starting deployment. The command requests `max_gen_toks=262144`, using [Qwen3.5's native total context size](https://huggingface.co/Qwen/Qwen3.5-35B-A3B#model-overview) as the ceiling.

```bash
foretoken eval examples/rsi/qwen3.5-35b-a3b --name qwen-rsi-muxi \
  --evaluator lm-eval --tasks gsm8k --num_fewshot 0 --limit 20 \
  --model_args num_concurrent=64,max_gen_toks=262144 --output experiment,plot,wandb \
  --output-dir results/qwen3.5-35b-a3b-muxi --iteration baseline
```
