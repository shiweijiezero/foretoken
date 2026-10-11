# Qwen Throughput and Decode Latency

English | [简体中文](qwen-decode_zh.md)

```text
Models: Qwen3.5-35B-A3B
Precision: fixed BF16
Deployment layout: agent-selected
Per-service configuration: at most 8 GPUs per service; agent-selected GPU count within this limit, parallelism, CPU cores and memory
Available resources: 2*8 MetaX C500 GPUs; determine and record available CPU cores and memory from the execution environment
Maximum parallel experiments: have the agent determine the limit from available resources and per-experiment requirements, and record it in the experiment notes
Evaluation workloads: measure concurrency 1 and 16 separately per service, covering input/output lengths and natural answers; agent-selected datasets and lengths
Optimization objectives: improve single-request decode speed and total output throughput (tokens/s) at concurrency 16, and reduce decode latency (TPOT) in both groups
Allowed changes: unrestricted
Work and completion conditions: optimize autonomously for 12 hours from the start of task execution
Deliverables: full iteration history with motivation, approach and changes, measurements, analysis, time spent and next decision for each iteration; per-workload gains, retained approach, code changes and result links
```

See [optimization tasks](README.md#autonomous-execution) for execution and resource scheduling, and the [experiment records guide](../experiment-records.md) for recording results.
