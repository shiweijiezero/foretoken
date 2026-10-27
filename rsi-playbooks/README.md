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
| Agent settings | Concurrent Explorer, Worker and Experimenter counts; Observers per check and the check interval. |

### 3. Start the agents

The commands below use the NVIDIA task. Replace the path to use your own task file.

The checkout includes project hooks for [RSI continuation](../.agents/skills/rsi/continuation.md). They require Python 3.11+ and affect only RSI sessions activated by the lead.

#### Claude Code

```bash
CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 claude
```

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

## Follow progress

The running `report.md` summarizes comparisons, performance and quality results, retained approaches and open validation questions. Follow its links for raw data and detailed analysis. See the [experiment records guide](experiment-records.md) for record formats.

To adjust objectives, budgets or the allowed scope, state the change in the conversation and update the task file.

To pause, use `/goal clear` in Claude Code or `/goal pause` in Codex, and ask the team to stop experiments, collectors and check reminders. In Codex, use `/goal resume` to continue with the original task and existing results.

## Architecture and roles

The task file expresses user requirements. The RSI skill and role instructions organize agent work. Foretoken provides builds, deployment, evaluation and profiling. Results inform the team's next step and the user-facing report.

| Role | Main responsibility |
| --- | --- |
| Lead | Choose research directions, allocate resources, make decisions and organize delivery. |
| Explorer | Search local or online sources, study mechanisms, and propose methods and questions. |
| Worker | Implement approaches, analyze results and improve the code or configuration. |
| Experimenter | Prepare environments, deploy and run experiments, save results and release resources. |
| Observer | Use a new agent each round to read records independently, check progress, research depth and resource use item by item, and suggest adjustments. |

## Execution rules and references

| Reader and purpose | Reference |
| --- | --- |
| Users: write or adjust a task | [Task template](templates/task-template.md) · [Existing tasks](tasks/README.md) |
| Agents: execution and role requirements | [RSI skill](../.agents/skills/rsi/SKILL.md) · [Team guidance](../.agents/skills/rsi/roles/common.md) |
| Agents: research methods and technical experience | [Research guidance](guidance/README.md) · [Knowledge library](guidance/knowledge/README.md) |
| Deployment operators: source updates and environment reuse | [Source deployment](../docs/deployment/source.md) |
| Experiment executors: performance, quality and profiling | [Evaluation guide](../benchmarks/README.md) · [Experiment recipes](../benchmarks/docs/recipes.md) |
| Users and agents: read and maintain results | [Experiment records guide](experiment-records.md) |
