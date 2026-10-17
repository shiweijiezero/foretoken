# Qwen Throughput and Decode Latency

English | [简体中文](qwen-decode_zh.md)

```text
Models: Qwen3.5-35B-A3B
Starting configuration: examples/rsi/qwen3.5-35b-a3b; BF16 TP4 with four-token MTP, adjustable during optimization
Optimization objectives: improve single-request decode speed and total output throughput (tokens/s) at concurrency 16, and reduce decode latency (TPOT) in both groups
Precision: fixed BF16
Deployment layout: agent-selected within the final single-node GPU budget, including parallelism and any stage separation
Final deployment budget: 4 GPUs on one node; at most 32 CPU cores and 512 GiB memory in total for the model and frontend workloads
Experiment sizing: service candidates fit the final budget; focused kernel, compilation, or correctness experiments may use fewer resources, including one GPU
Available resources: 2 nodes, each with 8 available MetaX C500 GPUs, 64 CPU cores, and 1 TiB memory
Minimum concurrently working subagents: 6
Maximum concurrently working subagents: 20
Minimum parallel experiments: 4 independent experiment groups across the two nodes
Maximum parallel experiments: not fixed at four; determine and record the limit from actual GPU, CPU, memory, and storage needs, including smaller experiments
Parallel scheduling: plan two four-GPU candidates per node, or more smaller experiments. If fewer than four groups can run, record and address the preparation or resource constraints.
Evaluation workloads: measure concurrency 1 and 16 separately per service using random-length workloads and StudyChat conversations; see the full commands under "Evaluation commands" below
Accuracy evaluation: run foretoken eval as needed for changes that may affect accuracy, at most once per 20 completed performance iterations
Allowed changes: implementation and configuration at any layer, within the precision and resource constraints above
Measurement budget per iteration: default to 10 minutes of perf/eval measurement to keep iterations fast; adjust the measurement budget and request count to the actual workload, recording the reason and time spent
Work duration: a total budget of 24 hours from the start of task execution
Expected outcome / completion criteria: maximize throughput and minimize TPOT without fixed metric thresholds; continue diagnosis, online research, and optimization experiments until measurements confirm no further optimization opportunities, the time budget expires, or the user explicitly asks to stop
Deliverables (<iteration> is the approach name passed to --iteration):
  - Changes and commands: record each iteration's code/configuration changes and full foretoken perf commands in the "Changes and commands" section of results/qwen-decode/iterations/<iteration>/notes/iteration.md.
  - Performance metrics: record output throughput (tokens/s) and mean TPOT (ms/token) at concurrency 1 and 16 for each workload in the "Performance metrics" table in the same iteration.md.
  - Comparative analysis: record values and percentage changes against the initial approach and previous iteration under the same workload, with explanations, in the "Comparative analysis" section of the same iteration.md.
  - Evidence: record W&B links, plots, and raw result paths in the "Evidence" section of the same iteration.md; retain automatically saved run artifacts under results/qwen-decode/iterations/<iteration>/runs/.
  - Execution efficiency: record the time-weighted mean number of subagents actively executing tasks over the full interval from task start to finish, and report mean GPU utilization separately for both nodes in the "Execution efficiency" section of results/qwen-decode/notes/experiment.md. State the observation interval, resource scope, sampling coverage, and reasons for low utilization, with references to raw records.
  - Final approach: record the retained approach, its code/configuration, per-workload measurements, and an iteration comparison summary in results/qwen-decode/notes/experiment.md.
  - User-facing summary: create rsi-playbooks/tasks/qwen-decode/<experiment>/ using the experiment name. Use rsi-playbooks/templates/report-template.md to write that directory's report.md for readers who did not participate, presenting iteration changes, measured metrics and changes from base, conclusions, execution efficiency, and W&B result links; place key plots in figures/ and reference them with relative paths. Deliver the summary file path.
```

Follow the [RSI skill](../../.agents/skills/rsi/SKILL.md) for execution and resource scheduling, and the [experiment records guide](../experiment-records.md) for recording results.

## Starting deployment

The example enables four-token multi-token prediction (MTP): the model verifies draft tokens before returning them. It uses asynchronous scheduling and MetaX runtime settings while retaining graph acceleration and automatic KV cache sizing. The cache volume starts at 128Gi and can grow automatically to 256Gi when its StorageClass supports online expansion.

Use the [four-GPU starting configuration](../../examples/rsi/qwen3.5-35b-a3b/) on a MetaX-enabled Foretoken platform with a compatible runtime, Gateway frontend access, and a default StorageClass supporting `ReadWriteOnce`. Deploy with `foretoken deploy examples/rsi/qwen3.5-35b-a3b --timeout 60m`, then evolve it within the task budget. Use comparable four-GPU results to judge service-level gains; smaller probes answer local questions and do not replace the final service evaluation. Validate the retained approach without competing experiment load before delivery.

## Evaluation commands

Run from the repository root. These commands use the starting deployment; substitute each candidate's own deployment directory when comparing approaches. Authenticate with `wandb login` before using W&B. Results are saved under `results/qwen-decode/` on the execution machine. Use `baseline` for the initial approach, rename it for a new approach, and reuse it for repeat measurements of the same approach. The agent writes the notes above; commands generate metrics and plots.

Each performance loop below measures concurrency 1 and 16 sequentially, using the same inputs, 80 measured requests and four warmup requests per group. StudyChat is required for the final retained approach and optional during intermediate iterations. The GSM8K command allows up to 64 concurrent API requests; the engine schedules them within its sequence limit. See [recipes](../../benchmarks/docs/recipes.md) for more workloads.

```bash
# Every performance iteration: random lengths at concurrency 1 and 16.
for concurrency in 1 16; do
  foretoken perf examples/rsi/qwen3.5-35b-a3b --dataset random \
    --min-prompt-length 128 --max-prompt-length 4096 \
    --min-output-length 128 --max-output-length 512 \
    --max-concurrency "$concurrency" --num-prompts 80 --warmup-requests 4 \
    --random-seed 0 --temperature 0 --output experiment,raw,plot,wandb \
    --output-dir results/qwen-decode --iteration baseline
done

# Required for the final retained approach: StudyChat at concurrency 1 and 16.
for concurrency in 1 16; do
  foretoken perf examples/rsi/qwen3.5-35b-a3b \
    --dataset hf://datasets/KrisQ/StudyChat/data.jsonl \
    --max-concurrency "$concurrency" --num-prompts 80 --max-tokens 2048 \
    --warmup-requests 4 --temperature 0 --output experiment,raw,plot,wandb \
    --output-dir results/qwen-decode --iteration baseline
done

# Accuracy evaluation as needed, with up to 64 concurrent requests.
foretoken eval examples/rsi/qwen3.5-35b-a3b \
  --evaluator lm-eval --tasks gsm8k --num_fewshot 0 --limit 20 \
  --model_args num_concurrent=64 --output experiment,plot,wandb \
  --output-dir results/qwen-decode --iteration baseline
```
