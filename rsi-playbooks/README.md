# RSI Playbooks

English | [简体中文](README_zh.md)

Foretoken is an inference infrastructure framework designed for RSI (Recursive Self-Improvement), enabling agents to autonomously modify, deploy, evaluate, and optimize inference systems.

These playbooks provide task guidance, experimental methods, and reusable knowledge. Validated findings are incorporated to make subsequent optimization work more effective.

## Start an optimization task

Clone the repository:

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken
```

### Claude Code

Start a dedicated interactive session from this directory:

```bash
CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 claude
```

This enables Agent Teams for this session. Paste the following into that session, or one with Teams already enabled. Replace the file path to use your own task:

```text
/goal Use the RSI skill and an Agent Team to complete the inference-system optimization in rsi-playbooks/tasks/qwen-decode.md and deliver its required results. Read the task, prior experiment records, and relevant guidance under rsi-playbooks/guidance/. Create a dedicated Git worktree for this task and separate worktrees for parallel candidates. As lead, develop and revise directions, assign research questions, and synthesize findings. Own environment preparation, deployment and parallel experiments, resource cleanup, and final integrated acceptance. Workers research and develop candidates, explain their changes, and analyze results you return rather than running code themselves. Execute each delivered candidate from its corresponding checkout, keeping running source unchanged. Reuse recorded worktrees after checking their state on resume, and keep the team collaborating as findings arrive. Maintain the task's required number of actively working agents and respond to results and requests for new work. Follow the task's resources, time budget, and end conditions; on a user pause or stop, halt further work and arrange the necessary cleanup.
```

The lead uses one goal for the overall outcome and team progress, while members work within their roles. To pause, use `/goal clear` to end automatic continuation, then ask the team to stop this task's experiments and collectors.

### Codex

Open Codex in the repository and paste:

```text
/goal Use the RSI skill to complete the inference-system optimization in rsi-playbooks/tasks/qwen-decode.md and deliver its required results. Read the task, prior experiment records, and relevant guidance under rsi-playbooks/guidance/. Create a dedicated Git worktree for this task and separate worktrees for parallel candidates. As lead, develop and revise directions, assign research questions, and synthesize findings. Own environment preparation, deployment and parallel experiments, resource cleanup, and final integrated acceptance. Workers research and develop candidates, explain their changes, and analyze results you return rather than running code themselves. Execute each delivered candidate from its corresponding checkout, keeping running source unchanged. Reuse recorded worktrees after checking their state on resume, and keep the team collaborating as findings arrive. Maintain the task's required number of actively working agents and respond to results and requests for new work. Follow the task's resources, time budget, and end conditions; on a user pause or stop, halt further work and arrange the necessary cleanup.
```

Codex uses its native `/goal` to maintain the task objective and its agent tools for delegation and follow-up. Use `/goal pause` to pause the goal and ask members to stop the task's experiments and collectors; use `/goal resume` to continue.

## Agent roles

Multiple Workers advance optimization directions in parallel, supported by the other roles. The lead sets staffing from task resources and parallelism requirements and retains member context for follow-up work:

| Role | Responsibility and staffing |
| --- | --- |
| Lead | Develops and revises directions, assigns work, reviews candidate code, and synthesizes decisions. Owns environment preparation, deployment and parallel experiments, resource cleanup, integrated validation, and final delivery. |
| Advisor | At least two members focus on research, bottleneck analysis, exploration, feedback, and reflection. They understand the code and system, search community sources for ideas, and challenge local assumptions with broader evidence. The lead sets staffing within the budget and harness limits. |
| Worker | Researches an assigned direction, identifies bottlenecks, designs and implements changes, and analyzes results returned by the lead. Delivers code with explanations; execution belongs to the lead. At least one Worker follows the system's end-to-end execution path. Prefer one or more dedicated Workers per active direction; the lead determines total staffing. |
| Recorder | One by default. Independently reviews candidate behavior, comparison conditions, and gain attribution; maintains iteration history, the current overview, evidence links, and the final report. Does not implement the candidates it reviews. |
| Monitor | One supervisor covers the whole team's work efficiency and resource utilization. Periodically checks tasks, member progress, and useful parallelism, maintains continuous GPU collection, and follows up on delayed handoffs, duplicate work, blockers, or idle resources. |

Member names combine a role with a number or codename, such as `lead-01`, `advisor-01`, `worker-01`, `recorder-01`, `monitor-01`, or `worker-alpha`. Names stay stable across assignments; responsibilities belong in task descriptions. The [shared skill](../.agents/skills/rsi/SKILL.md#set-up-the-team-and-assignments) defines naming and assignment details.

Working-agent counts include Workers, Advisors, and Recorders actually doing assigned work, counted once per member. Exclude the lead and Monitor; waiting, blocked, or completed members do not count as working. The lead arranges useful work within task and harness limits and addresses staffing shortfalls.

Assign each Worker a research question, scope, budget, expected development outcome, and checkout. Workers research and develop independently without waiting for Advisor proposals. Advisors and Recorders collaborate directly with them; the lead executes delivered changes and returns results for analysis and further development. Review and recording overlap independent preparation. The lead selects retained changes and owns integrated validation and delivery.

Members report findings that affect others and request follow-up when their assignment is complete or cannot advance. The Recorder owns the overview and iteration notes, linking original evidence rather than duplicating it. On resumption, preserve the original budget, reconcile records with actual results and processes, and recreate missing members.

### Parallel workspaces

Use independent Git worktrees (separate working directories of the same repository) for parallel candidates. The lead prepares worktrees and coordinates the base revision, writers, resources, and result locations. Workers edit their assigned candidates; the lead synchronizes, builds, deploys, and measures each delivered version from its corresponding checkout, using distinct deployment identities, writable caches, and result directories. Keep that checkout unchanged while it is running; continue development in another worktree or after the lead releases it. Read-only research and review can read the relevant checkout directly.

Experiment counts and GPU allocations come from the task. Reviewers examine each candidate's actual code and results; after selecting retained changes, the lead arranges integration and validation. Remove worktrees once changes and evidence are preserved and their users have finished.

## Design goals

- Rapid deployment: apply code changes quickly to inference services in the target cluster.
- Testing and feedback: measure performance, model quality, and resource costs to evaluate the effects of a change.
- Observation and diagnosis: use logs, metrics, and profiling to locate runtime problems and performance bottlenecks.
- Experiment records: connect each iteration's changes, configurations, results, and decisions for comparison and reproduction.
- References and shared experience: find and update references, and turn experimental findings into reusable guidance.
- Behavior and attribution checks: examine actual model behavior, identify metric gaming, and determine whether gains come from the intended change.

## Steps

A research question may span several candidate iterations or conclude with evidence that an approach is unsuitable. An iteration evaluates one candidate; its workload groups and repeats belong together. Completion of the overall task follows its objectives, budget, and end conditions.

1. Define the behavior to improve, scope, and success criteria. Read relevant code, research, and prior results to identify a question worth investigating.
2. Advisors and Workers develop hypotheses from research and existing evidence. The lead selects comparisons and resolves execution feasibility before committing to larger work.
3. Workers implement viable candidates and explain the changes. The lead personally reads the changes and relevant call paths, reviews implementation correctness, architectural fit, and activation conditions, and discusses implementation issues with the Worker. From that review, the lead chooses comparisons and validation, builds and deploys through the [source deployment workflow](../docs/custom-deployment.md), and uses targeted diagnostics as needed to confirm the intended execution path before formal measurements.
4. The lead runs selected [evaluations](../benchmarks/README.md), using existing [sweeps](../benchmarks/docs/recipes.md) where applicable and keeping comparison conditions stable. Workers analyze returned outputs, logs, and profiles, discuss findings with Advisors, and refine their implementations.
5. Have an independent Recorder review the implementation and original results for behavior, comparison conditions, and attribution. Address questions while unrelated preparation continues, and preserve the evidence and conclusions in the [experiment records](experiment-records.md).
6. Refine or discard experimental candidates as evidence warrants. The lead selects reviewed changes for the retained approach and arranges combined service validation. Feed reusable findings into guidance and choose the next question within the remaining task budget.

## Choose evaluations

Select evaluations for the current optimization goal. See the [experiment records guide](experiment-records.md) for commands and recording options.

| Evaluation goal | Tools and commands | Results to inspect |
| --- | --- | --- |
| Compare latency, decode speed, and throughput | [Performance evaluation](../benchmarks/docs/perf/README.md), `foretoken perf` | Success count, latency distribution, TPOT, throughput, and actual output length |
| Evaluate answer quality | [Quality evaluation](../benchmarks/docs/eval/README.md), `foretoken eval` | Scores, sample counts, scoring method, and individual answers |
| Locate computation, communication, and waiting costs | [Profiling](../benchmarks/docs/profile/README.md), `foretoken perf --profile` | Execution timeline; use separate unprofiled runs for speed comparisons |
| Compare concurrency, input lengths, and traffic patterns | [Sweep and workload configurations](../benchmarks/docs/recipes.md) | Metrics and trends across workload points |

Inspect command options with `foretoken perf --help` and `foretoken eval --help`.

## Task entry points

Choose a task from [optimization tasks](tasks/README.md), and consult [shared guidance](guidance/README.md) for references and prior findings.
