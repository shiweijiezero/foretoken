# RSI Inference Optimization

English | [简体中文](README_zh.md)

Recursive self-improvement (RSI) uses agents to investigate and improve inference systems: research an approach, change code or configuration, deploy it, measure its effects, and use the results to guide further work. Foretoken provides the deployment, evaluation, and profiling tools for this cycle. Task files define the model, resources, budget, and deliverables; the [guidance library](guidance/README.md) provides research methods and findings from earlier work.

## Start an optimization task

Install and sign in to Claude Code or Codex, then get the repository's tasks and examples:

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken
```

The examples below use the [Qwen3.5-35B-A3B optimization task](tasks/qwen3.5-35b-a3b.md). Check that its required resources are available before starting. For your own task, use the [task template](templates/task-template.md) and replace the file path in the prompt.

### Claude Code

Start an interactive session with Agent Teams enabled:

```bash
CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 claude
```

Enter:

```text
/goal Use the RSI skill and an Agent Team to complete rsi-playbooks/tasks/qwen3.5-35b-a3b.md. Read prior experiment records and relevant guidance, follow the task's resources, time budget and end conditions, and deliver the required results.
```

To pause, use `/goal clear` to end automatic continuation and ask the team to stop this task's experiments and collectors.

### Codex

Open Codex in the repository and enter:

```text
/goal Use the RSI skill to complete rsi-playbooks/tasks/qwen3.5-35b-a3b.md. Read prior experiment records and relevant guidance, follow the task's resources, time budget and end conditions, and deliver the required results.
```

Use `/goal pause` to pause the goal and ask members to stop experiments and collectors. Use `/goal resume` to continue.

## Agent roles

The lead coordinates the task, choosing research directions and allocating resources from the evidence available. It combines the team's findings, decides which changes to retain, and organizes final validation and delivery.

Advisors continually investigate mechanisms and optimization methods, uncover new questions and reassess the current direction. Their research can deepen an existing investigation or explore ideas beyond the current candidate list.

Workers turn specific questions into code or configuration changes that can be evaluated, then improve them using the results.

Experimenters build and evaluate those approaches while observing resource use, recording results and cleaning up afterward.

Preparing the next candidate can overlap evaluation of the current one. The team adjusts concurrency in each stage from available resources and observed durations.

See the [RSI skill](../.agents/skills/rsi/SKILL.md) for assignments, workspace isolation and resource handoffs.

## Reuse the environment across iterations

Keep each candidate's source directory and instance name across iterations. For code experiments, [install the source platform](../docs/deployment/source.md#install-from-source) once; the same command can update an existing published platform. Parameter-only experiments can use the applications already installed.

| What changed? | How to update |
| --- | --- |
| Go, Rust or Python application code | Run `foretoken deploy` with `-e` pointing to the candidate's source directory. Unchanged environments and compatible build caches are reused. |
| Runtime dependencies or the base image | Redeploy with `-e` from the updated source directory to rebuild the affected environment. |
| Shared CRDs or platform configuration | Run `foretoken install -e .` from the source directory. |

After each change, deploy the candidate, confirm a successful request, then measure performance and answer quality. Use the same `--name` for deployment and evaluation: `perf` and `eval` measure the deployed service without updating its code. Use the results to decide what to retain or try next.

For example, run these commands from the candidate checkout to update and measure the single-GPU Quick Start. For another task, use its deployment directory and benchmark workload:

```bash
foretoken deploy examples/quickstart/qwen3-0.6b \
  --name candidate-a -e . --timeout 20m
foretoken perf examples/quickstart/qwen3-0.6b \
  --name candidate-a --num-prompts 20 --output local
```

The benchmark saves results under `results/`.

Environment and cache reuse usually make later code updates faster than initial setup. Record setup time separately from each iteration's build, publication, startup and measurement time.

For [parallel candidates](../docs/deployment/source.md#run-candidates-in-parallel), share model downloads through the configured storage and keep runtime caches and results separate. To release GPUs between iterations while retaining the instance and its caches:

```bash
foretoken stop candidate-a
```

Resume with the same deployment command. When the candidate is no longer needed, remove it with `foretoken delete candidate-a`.

Use existing sweeps for fixed workload groups, and validate combined changes before delivery. See [exploration and decision-making](guidance/meta-knowledge/03-exploration-and-decision.md) for choosing experiments and interpreting results.

## Choose evaluations

| Goal | Entry point |
| --- | --- |
| Measure latency and throughput | [`foretoken perf`](../benchmarks/docs/perf/README.md) |
| Evaluate answer quality | [`foretoken eval`](../benchmarks/docs/eval/README.md) |
| Locate execution bottlenecks | [`foretoken perf --profile`](../benchmarks/docs/profile/README.md) |
| Compare concurrency, input lengths and other conditions | [Sweeps and experiment commands](../benchmarks/docs/recipes.md) |

Inspect options with `foretoken perf --help` and `foretoken eval --help`.

## Read results

The experiment report (`report.md`) compares performance across approaches, explains the findings and identifies the currently selected approach. It is updated as the experiment progresses, with links to detailed experiment records and original data. The [experiment records guide](experiment-records.md) covers output options, saved files, and report maintenance.

See [optimization tasks](tasks/README.md) for more tasks and the [knowledge library](guidance/knowledge/README.md) for reusable technical findings.
