# RSI Inference Optimization

English | [简体中文](README_zh.md)

Recursive self-improvement (RSI) uses agents to research, implement and validate inference optimizations. Users define goals and boundaries in a task file. The team uses Foretoken to deploy, evaluate and profile approaches, then uses the results to guide further work.

## Start an optimization task

### 1. Prepare the repository and task file

Install and sign in to Claude Code or Codex, then start the task in a Foretoken checkout. If you do not have one:

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken
```

Start with the [Qwen3.5-35B-A3B NVIDIA task](tasks/qwen3.5-35b-a3b-nvidia.md). Other hardware tasks are listed in [existing tasks](tasks/README.md). To define your own, use the [task template](templates/task-template.md) and save it, for example, as `my_docs/rsi/my-task.md`. The `my_docs/` directory is ignored by Git.

### 2. Set the task requirements

Confirm the following settings. Leave choices to the agents where you do not need a restriction; the task template explains how to fill them in.

| Setting | What to specify |
| --- | --- |
| Model and objective | Which model to optimize and whether throughput, latency or another metric matters most. |
| Available resources | Nodes or clusters the task may access, and the GPU, CPU and memory available for all experiments. |
| Final deployment budget | Resources allowed for the final service, separate from the total experiment resources. |
| Evaluation and constraints | Workloads, concurrency, precision, quality requirements and the code or configuration that may change. |
| Time and delivery | Duration or completion conditions, prior experiments, and where to save reports and results. |
| Agent settings | Concurrent Explorer, Worker and Experimenter counts; Evaluators per check and the check interval. |

### 3. Start the agents

The commands below use the NVIDIA task. Replace the path to use your own task file.

The checkout includes project hooks for [RSI continuation](../.agents/skills/rsi/continuation.md). They require Python 3.11+ and affect only explicitly activated RSI tasks.

#### Claude Code

```bash
CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 claude
```

The project settings enable Claude Code's Ultracode dynamic workflows. This is separate from Agent Teams and from the effort level; keep the model and effort mapping in the task file.

Enter in the session:

```text
/goal Use the RSI skill and an Agent Team to complete rsi-playbooks/tasks/qwen3.5-35b-a3b-nvidia.md.
```

#### Codex

In the Codex desktop app, open the repository and review the Foretoken project hooks under Settings → Hooks. If using the terminal CLI, run `codex` from the repository and review the hooks with:

```text
/hooks
```

After trusting the project hooks, start the goal in the Codex session:

```text
/goal Use the RSI skill to complete rsi-playbooks/tasks/qwen3.5-35b-a3b-nvidia.md.
```

If Codex cannot create the requested team because of its agent limit, set a suitable cap in the user configuration, `~/.codex/config.toml` (or `$CODEX_HOME/config.toml` when `CODEX_HOME` is set):

```toml
[agents]
max_concurrent_threads_per_session = 16
```

Choose enough slots for concurrent Explorers, Workers and Experimenters plus an Evaluator round.

## Follow progress

Each Experimenter records its own results and dispositions; the existing plot entry point aggregates comparisons and progress. The report links measured gains, retained approaches and open validation questions to their originals. See the [experiment records guide](experiment-records.md) for record formats.

To adjust objectives, budgets or the allowed scope, state the change in the conversation. The lead updates the task record and affected work arrangements.

To pause, use `/goal clear` in Claude Code or `/goal pause` in Codex, and ask the team to stop experiments, collectors and check reminders. In Codex, use `/goal resume` to continue with the original task and existing results.

## Architecture and roles

The task file expresses user requirements. The RSI skill and role instructions organize agent work. Foretoken provides builds, deployment, evaluation and profiling. Results inform the team's next step and the user-facing report.

| Role | Main responsibility |
| --- | --- |
| Root agent | Establish the task, observe records, direct corrections to owners and briefly update the user while members work independently. |
| Explorer | Search local or online sources, study mechanisms, and propose methods and questions. |
| Worker | Own implementation, interpret results and continue or retain approaches within the task authority. |
| Experimenter | Claim ready experiments, reuse environments, save original results and release resources. |
| Evaluator | Independently inspect changes and open issues, delivering actionable findings directly to their owners. |

## Execution rules and references

| Reader and purpose | Reference |
| --- | --- |
| Users: write or adjust a task | [Task template](templates/task-template.md) · [Existing tasks](tasks/README.md) |
| Agents: execution and role requirements | [RSI skill](../.agents/skills/rsi/SKILL.md) · [Team guidance](../.agents/skills/rsi/roles/common.md) |
| Agents: research methods and technical experience | [Research guidance](guidance/README.md) · [Knowledge library](guidance/knowledge/README.md) |
| Deployment operators: source updates and environment reuse | [Source deployment](../docs/deployment/source.md) |
| Experiment executors: performance, quality and profiling | [Evaluation guide](../benchmarks/README.md) · [Experiment recipes](../benchmarks/docs/recipes.md) |
| Users and agents: read and maintain results | [Experiment records guide](experiment-records.md) |
