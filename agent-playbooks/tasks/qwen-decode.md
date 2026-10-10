# Qwen Throughput and Decode Latency

English | [简体中文](qwen-decode_zh.md)

```text
Execution entry: current Foretoken working directory and the task's selected Kubernetes context
Previous experiments: continue from earlier Qwen3.5-35B-A3B BF16 decode experiments, checking workload and runtime conditions, reusing comparable results and pursuing open questions; take initial measurements if no records exist
Models: Qwen3.5-35B-A3B
Precision: fixed BF16
Deployment layout: agent-selected
Per-service configuration: agent-selected GPU count, parallelism, CPU cores and memory based on measurements
Available resources: 2*8 MetaX C500 GPUs; determine and record available CPU cores and memory from the execution environment
Maximum parallel experiments: have the agent determine the limit from available resources and per-experiment requirements, and record it in the experiment notes
Evaluation workloads: measure concurrency 1 and 16 separately per service, covering input/output lengths and natural answers; agent-selected datasets and lengths
Optimization objectives: preserve answer quality while improving single-request decode speed and total output throughput (tokens/s) at concurrency 16, and reducing decode latency (TPOT) in both groups; report changes and trade-offs against the original approach separately
Allowed changes: unrestricted
Work and completion conditions: use the total work period or performance target specified when assigning the task
Deliverables: full iteration history with motivation, approach and changes, measurements, analysis, time spent and next decision for each iteration; per-workload gains, retained approach, code changes and result links
```

See [optimization tasks](README.md#autonomous-execution) for execution and resource scheduling, and the [experiment records guide](../experiment-records.md) for recording results.
