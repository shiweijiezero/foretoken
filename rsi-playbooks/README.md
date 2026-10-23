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

For code optimization, prepare the source platform once; parameter-only experiments can reuse installed applications. Keep each candidate's checkout and instance name across iterations. The normal loop is: choose a question → change code or configuration → deploy the candidate → confirm a successful request → measure performance and quality → retain or revise the approach.

| Situation | Workflow |
| --- | --- |
| First source experiment | Follow [source installation](../docs/deployment/source.md#install-from-source) to prepare the platform, toolchains and runtime environments. An existing published platform can be converted through the same installation command. |
| Go, Rust or Python application changes | Use `foretoken deploy` with `-e` pointing to the candidate checkout. Reuse unchanged environments and compatible build caches, and publish the affected applications rather than rebuilding and transporting complete runtime images each round. |
| Runtime dependency, base-image or shared platform changes | Rebuild the affected environment through the source workflow; use `foretoken install -e` for shared CRD or platform changes. Return to incremental deployment for subsequent application edits. |

Reusing the environment makes repeated code updates faster than first-time preparation. Keep initial setup time separate from each iteration's build, publication, service startup and measurement time. Model processes may still need to restart or reload weights after serving-code changes.

Use [parallel source deployments](../docs/deployment/source.md#run-candidates-in-parallel) for independent candidates, with the same `--name` in deployment and evaluation commands. Run `deploy` after changes before starting `perf` or `eval`: benchmarks reuse an existing service without updating its code. Share model downloads through the configured storage, while keeping each instance's runtime caches and results separate. Use `stop` to release GPUs between iterations; delete instances when they are no longer needed.

Once workload groups are defined, compare candidates through existing sweeps. Use the results to continue, change direction or combine approaches, and validate the retained combination before delivery. See [exploration and decision-making](guidance/meta-knowledge/03-exploration-and-decision.md) for choosing experiments and interpreting results.

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
