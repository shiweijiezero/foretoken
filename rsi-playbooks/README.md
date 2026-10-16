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
/goal Use the RSI skill and an Agent Team to complete the inference-system optimization in rsi-playbooks/tasks/qwen-decode.md and deliver its required results. Read the task, prior experiment records, and relevant guidance under rsi-playbooks/guidance/. Use task branches and independent checkouts for parallel writers. As lead, develop directions, allocate resources, coordinate shared environment changes, and own integration and final acceptance. Give each Worker a complete research question and relevant evidence. Assign one execution owner per candidate, with explicit resources and comparison conditions; that owner may iterate within the assignment without waiting for each command. Keep running source unchanged. Reconcile existing jobs and allocations on resume, and keep the team collaborating as findings arrive. Maintain the task's required number of actively working agents and respond to results and requests for new work. Follow the task's resources, time budget, and end conditions; on a user pause or stop, halt further work and arrange the necessary cleanup.
```

The lead uses one goal for the overall outcome and team progress, while members work within their roles. To pause, use `/goal clear` to end automatic continuation, then ask the team to stop this task's experiments and collectors.

### Codex

Open Codex in the repository and paste:

```text
/goal Use the RSI skill to complete the inference-system optimization in rsi-playbooks/tasks/qwen-decode.md and deliver its required results. Read the task, prior experiment records, and relevant guidance under rsi-playbooks/guidance/. Use task branches and independent checkouts for parallel writers. As lead, develop directions, allocate resources, coordinate shared environment changes, and own integration and final acceptance. Give each Worker a complete research question and relevant evidence. Assign one execution owner per candidate, with explicit resources and comparison conditions; that owner may iterate within the assignment without waiting for each command. Keep running source unchanged. Reconcile existing jobs and allocations on resume, and keep the team collaborating as findings arrive. Maintain the task's required number of actively working agents and respond to results and requests for new work. Follow the task's resources, time budget, and end conditions; on a user pause or stop, halt further work and arrange the necessary cleanup.
```

Codex uses its native `/goal` to maintain the task objective and its agent tools for delegation and follow-up. Use `/goal pause` to pause the goal and ask members to stop the task's experiments and collectors; use `/goal resume` to continue.

## Agent roles

Multiple Workers advance optimization directions in parallel, supported by the other roles. The lead sets staffing from task resources and parallelism requirements and retains member context for follow-up work:

| Role | Responsibility and staffing |
| --- | --- |
| Lead | Develops directions, assigns questions and resources, and selects retained changes. Coordinates shared environment changes and execution owners; personally reviews retained code and owns integrated validation and final delivery. |
| Advisor | At least two members focus on research, bottleneck analysis, exploration, feedback, and reflection. They understand the code and system, search community sources for ideas, and challenge local assumptions with broader evidence. The lead sets staffing within the budget and harness limits. |
| Worker | Researches, implements and analyzes an assigned question, and runs candidates when given an explicit execution allocation. At least one Worker follows the end-to-end path; the lead chooses staffing from useful independent work. |
| Experimenter | Builds and evaluates ready candidates, observes resource use, records execution facts and cleans up assigned resources. Scale with evaluation capacity; one designated writer maintains the overview and report. An uninvolved member independently reviews claims. |

Member names combine a role with a number or codename, such as `lead-01`, `advisor-01`, `worker-01`, `experimenter-01`, or `worker-alpha`. Names stay stable across assignments; responsibilities belong in task descriptions. The [shared skill](../.agents/skills/rsi/SKILL.md#set-up-the-team-and-assignments) defines naming and assignment details.

Working-agent counts include Workers, Experimenters, and Advisors actually doing assigned work, counted once per member. Exclude the lead; waiting, blocked, or completed members do not count as working, and combining roles does not add members. The lead arranges useful work within task and harness limits and addresses staffing shortfalls.

Assign each Worker a complete question, reference evidence, scope and budget. The [role contract](../.agents/skills/rsi/roles/common.md) defines bounded execution and resource handoff. Workers with an execution allocation can iterate locally while the lead handles shared changes and cross-direction decisions. Experimenters return results directly; Advisors contribute research and can independently review candidates they did not implement or execute. The lead reviews retained changes and owns integrated validation and delivery.

Members report findings that affect others and request follow-up when their assignment is complete or cannot advance. The designated Experimenter owns the overview, while each executor maintains assigned iteration notes, linking original evidence rather than duplicating it. On resumption, preserve the original budget, reconcile records with actual results and processes, and recreate missing members.

### Prepare candidates while evaluations run

Workers prepare source versions and comparisons; Experimenters consume ready candidates within allocated resources. Keep separate counts for candidates being prepared, waiting for evaluation and executing, using the same task records. Preparation and evaluation concurrency need not match. Prepare useful candidates ahead when evaluation resources would otherwise wait; reduce new preparation when the ready queue grows and prioritize analysis or higher-value alternatives. See the [handoff and backlog guidance](../.agents/skills/rsi/roles/common.md#separate-candidate-preparation-from-evaluation).

For a small task, one member may combine development and execution. For larger parallel work, Experimenters combine execution, resource observation and factual recording, while independent review remains a separate assignment rather than another mandatory resident role.

### Parallel workspaces

Use a task branch and give concurrent writers independent checkouts, such as separate clones. Different branches in one directory do not isolate simultaneous edits. The lead coordinates source bases, writers and execution allocations; each candidate owner uses distinct deployment identities, writable caches and result paths. Keep source unchanged during build and measurement, and preserve changes and evidence before releasing a checkout.

Use existing Kubernetes resource controls, sweeps, benchmark records and harness task notifications rather than a separate RSI scheduler. They handle placement, fixed run sequences and execution records; the lead still coordinates shared-resource contention and quiet comparison windows. Namespace separation does not guarantee performance isolation. Details are in the [shared skill](../.agents/skills/rsi/SKILL.md#isolate-candidate-workspaces-and-execution).

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
3. Workers implement and explain candidates and comparisons. Within the agreed scope, the assigned execution owner builds and deploys through the [source deployment workflow](../docs/custom-deployment.md), checking that the intended execution path ran.
4. The execution owner runs selected [evaluations](../benchmarks/README.md), using existing [sweeps](../benchmarks/docs/recipes.md) for fixed groups and keeping comparison conditions stable. Workers analyze outputs, logs and profiles, exchange findings with Advisors, and continue within their assignment.
5. Have a member uninvolved in the candidate's implementation and execution review its code and original results for behavior, comparison conditions, and attribution. Address questions while unrelated preparation continues, and preserve the evidence and conclusions in the [experiment records](experiment-records.md).
6. Refine or discard experimental candidates as evidence warrants. The lead personally reads retained changes and relevant call paths, considers the independent review, and arranges combined service validation. Feed reusable findings into guidance and choose the next question within the remaining task budget.

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
