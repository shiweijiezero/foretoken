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

## Steps

The team reads the task, code and previous results, chooses a useful question, and investigates it through implementation and experiments. A question may take several iterations or lead to a finding that an approach is unsuitable. The lead uses the results to continue, change direction or combine approaches, then validates and delivers the outcome when the task ends.

Use [source deployments](../docs/deployment/source.md#run-candidates-in-parallel) to build code changes and give each parallel candidate its own instance name. Use the same `--name` with deployment and evaluation commands, and select that instance by name for `status`, `stop`, `delete` and `endpoint`.

Once workload groups are defined, the team can compare them through existing sweeps and assess performance and answer quality against the task's requirements. See [exploration and decision-making](guidance/meta-knowledge/03-exploration-and-decision.md) for choosing experiments, interpreting results, and continuing the research.

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
