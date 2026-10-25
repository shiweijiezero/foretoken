# Qwen3.5-35B-A3B Optimization

English | [简体中文](qwen3.5-35b-a3b_zh.md)

```text
Models: Qwen3.5-35B-A3B
Starting configuration: examples/rsi/qwen3.5-35b-a3b; BF16 TP4 with four-token MTP and 128G local CPU KV offload, adjustable during optimization
Optimization objectives: improve single-request decode speed and total output throughput (tokens/s) at concurrency 16, and reduce decode latency (TPOT) in both groups
Precision: fixed BF16
Deployment layout: agent-selected within the final single-node GPU budget, including parallelism and any stage separation
Final deployment budget: 4 GPUs on one node; at most 32 CPU cores and 250G host memory in total for the model and frontend workloads
Experiment sizing: service candidates fit the final budget; focused kernel, compilation, or correctness experiments may use fewer resources, including one GPU
Available resources: 2 nodes, each with 8 available GPUs, 64 CPU cores, and 1 TiB memory
Advisor count: 2
Worker count: 2
Experimenter count: 2
Minimum parallel experiments: 4 independent experiment groups across the two nodes
Maximum parallel experiments: not fixed at four; determine and record the limit from actual GPU, CPU, memory, and storage needs, including smaller experiments
Parallel scheduling: reserve capacity for platform services, builds and benchmark clients, then place four-GPU candidates and smaller experiments according to their resource requests. If fewer than four groups can run, record and address the preparation or resource constraints.
Evaluation workloads: measure concurrency 1 and 16 separately per service using random-length workloads and StudyChat conversations; see the full commands under "Evaluation commands" below
Accuracy evaluation: run foretoken eval as needed for changes that may affect accuracy, at most once per 20 completed performance iterations
Allowed changes: implementation and configuration at any layer, within the precision and resource constraints above
Measurement budget per iteration: default to 10 minutes of perf/eval measurement to keep iterations fast; adjust the measurement budget and request count to the actual workload, recording the reason and time spent
Work duration: a total budget of 24 hours from the start of task execution
Expected outcome / completion criteria: keep increasing throughput and reducing TPOT without fixed metric thresholds.
  Continue researching methods, changing implementations and running experiments. Finish when the selected questions are answered and evidence shows that further work is not worthwhile, when the time budget expires, or when the user asks to stop.
  Explain why the task ended. Distinguish approaches tested without benefit, implementations that could not run, and ideas not yet implemented or tested. Finishing a parameter search alone does not show that no other optimization opportunities remain.
Deliverables (<iteration> is the approach name passed to --iteration):
  - Changes and commands: record each iteration's code/configuration changes and full foretoken perf commands in the "Changes and commands" section of results/qwen3.5-35b-a3b/iterations/<iteration>/notes/iteration.md.
  - Performance metrics: record output throughput (tokens/s) and mean TPOT (ms/token) at concurrency 1 and 16 for each workload in the "Performance metrics" table in the same iteration.md.
  - Comparative analysis: record values and percentage changes against the initial approach and previous iteration under the same workload, with explanations, in the "Comparative analysis" section of the same iteration.md.
  - Evidence: record W&B links, plots, and raw result paths in the "Evidence" section of the same iteration.md; retain automatically saved run artifacts under results/qwen3.5-35b-a3b/iterations/<iteration>/runs/.
  - Execution efficiency: record the time-weighted mean number of subagents actively executing tasks over the full interval from task start to finish, and report mean GPU utilization separately for both nodes in the report's "Execution efficiency" section. State the observation interval, resource scope, sampling coverage, and reasons for low utilization, with references to raw records.
  - User-facing report: create and continuously maintain rsi-playbooks/tasks/qwen3.5-35b-a3b/<experiment>/report.md using rsi-playbooks/templates/report-template.md and the report workflow in the experiment records guide. Include per-workload comparisons at concurrency 1 and 16, changes from base, the retained approach and its code/configuration, conclusions, execution efficiency, links to iteration notes and W&B results. Place key plots in figures/ beside the report and reference them with relative paths.
```

Follow the [RSI skill](../../.agents/skills/rsi/SKILL.md) for execution and resource scheduling, and the [experiment records guide](../experiment-records.md) for recording results.

## Starting deployment

Adapt the engine arguments and environment variables in the [four-GPU example](../../examples/rsi/qwen3.5-35b-a3b/) to the target hardware. Prepare a [source-installed platform](../../docs/deployment/source.md) with [access to the repository's shared `data/` directory](../../docs/features/model-storage.md#share-model-files-across-namespaces), then deploy from the candidate checkout:

```bash
foretoken deploy examples/rsi/qwen3.5-35b-a3b --name qwen-rsi -e . --timeout 60m
```

The model and frontend request and limit host memory to `246G` and `4G`, respectively. The model allocation includes `128G` of CPU KV offload in total across its four TP workers. Use comparable four-GPU service runs for the final comparison; smaller probes support diagnosis.

## Evaluation commands

Run from the repository root. These commands select the starting instance `qwen-rsi`; substitute each candidate's deployment directory and `--name` together when comparing approaches. Authenticate with `wandb login` before using W&B. Results are saved under `results/qwen3.5-35b-a3b/` on the execution machine. Use `baseline` for the initial approach, rename it for a new approach, and reuse it for repeat measurements of the same approach. The agent writes the notes above; commands generate metrics and plots.

Each performance loop below measures concurrency 1 and 16 sequentially, using the same inputs, 80 measured requests and four warmup requests per group. StudyChat is required for the final retained approach and optional during intermediate iterations. The GSM8K command allows up to 64 concurrent API requests; the engine schedules them within its sequence limit. See [recipes](../../benchmarks/docs/recipes.md) for more workloads.

```bash
# Every performance iteration: random lengths at concurrency 1 and 16.
for concurrency in 1 16; do
  foretoken perf examples/rsi/qwen3.5-35b-a3b --name qwen-rsi --dataset random \
    --min-prompt-length 128 --max-prompt-length 4096 \
    --min-output-length 128 --max-output-length 512 \
    --max-concurrency "$concurrency" --num-prompts 80 --warmup-requests 4 \
    --random-seed 0 --temperature 0 --output experiment,raw,plot,wandb \
    --output-dir results/qwen3.5-35b-a3b --iteration baseline
done

# Required for the final retained approach: StudyChat at concurrency 1 and 16.
for concurrency in 1 16; do
  foretoken perf examples/rsi/qwen3.5-35b-a3b --name qwen-rsi \
    --dataset hf://datasets/KrisQ/StudyChat/data.jsonl \
    --max-concurrency "$concurrency" --num-prompts 80 --max-tokens 2048 \
    --warmup-requests 4 --temperature 0 --output experiment,raw,plot,wandb \
    --output-dir results/qwen3.5-35b-a3b --iteration baseline
done

# Accuracy evaluation as needed, with up to 64 concurrent requests.
foretoken eval examples/rsi/qwen3.5-35b-a3b --name qwen-rsi \
  --evaluator lm-eval --tasks gsm8k --num_fewshot 0 --limit 20 \
  --model_args num_concurrent=64 --output experiment,plot,wandb \
  --output-dir results/qwen3.5-35b-a3b --iteration baseline
```
