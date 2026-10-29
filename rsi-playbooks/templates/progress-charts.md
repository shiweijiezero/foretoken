<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Plot RSI optimization progress

English | [简体中文](progress-charts_zh.md)

Each Experimenter records its own runs and scope-specific judgment. The existing plot command reads these records to produce the shared report tables and native milestone/search charts:

```bash
foretoken plot --progress results/my-experiment
```

Results appear in `results/my-experiment/figures/progress/`: `progress-summary.md` is the readable summary, `progress-summary.csv` contains statistics and judgments, and `progress-runs.csv` links the recorded run, configuration, environment and source evidence. The Markdown embeds the matching charts. Link it from the task's report; no separate report writer or manually copied metrics are needed.

## Record an approach

Keep `progress.yaml` beside each approach's `notes/iteration.md`. Start the initial configuration record with:

```yaml
label: Initial configuration
baseline: true
measurements:
  random-concurrency-1:
    runs: [../runs/perf-000001]
    status: valid
```

A later candidate records its actual measured parent and judgment:

```yaml
label: + QKV views
measurements:
  random-concurrency-1:
    runs: [../runs/perf-000001]
    parent: ../../baseline/runs/perf-000001
    status: valid
    retained: true
```

Paths are relative to the record. A run can name a command directory or a specific saved result directory. List additional comparable repetitions in `runs`; the existing result reader calculates their means, counts and standard deviations. Do not enter numeric measurements in this file. Link the explanation and independent review from `iteration.md`.

| Field | Meaning |
| --- | --- |
| `label` | The full measured configuration or change from its named parent. |
| `baseline` | Mark only the user's initial configuration, once per scope. Defaults to false. |
| `measurements` | A mapping from comparison scope to selected runs and judgment. |
| `runs` | Existing command/result directories for one approach and workload. |
| `status` | `valid`, `reference`, `incorrect`, `compile_failed` or `runtime_failed`. |
| `retained` | Whether this measured version is adopted for this scope. Defaults to false; requires `valid`. |
| `parent` | The actual measured parent command/result directory. Required for a retained candidate; not required for the original baseline. |

A scope such as `random-concurrency-1` identifies the workload and comparison condition. Separate incompatible protocols, cache conditions or hardware comparisons rather than pooling them as repetitions. The command checks known workload fields across a scope and its measured parents, requiring separate scopes for conflicting workloads. Hardware differences remain in the original environment evidence and do not by themselves reject a historical reference. Original-baseline percentages describe the recorded change; the table also retains the explicitly measured local parent and its delta. If that scope has no original baseline, values remain absolute and charts show that the baseline is unmeasured.

Use `reference` when measured speed is available but an applicable output or quality question is unresolved. References appear in the search view without entering the best-so-far curve or retained milestones. Failed judgments remain visible even when they have no metrics. Retention is recorded by the responsible owner from the applicable evidence; the command does not adopt the fastest point.

Each scope starts with “Measured retained branch tips”, linking the measured adopted endpoints to their actual runs, configuration and available source records. Use these branches to select an applicable parent for further work. An ancestor remains in the history but leaves the endpoint list when another retained version inherits it through recorded parent runs. Independent branches stay separate; the view neither ranks them by throughput nor declares a unique optimum.

The generated retained-version chart follows actual parent-run references. A and B measured independently from P remain separate branches; they do not establish A+B. If a measured parent is outside the plotted records, the retained point remains unconnected. The run table preserves known source/configuration references and missing evidence; a captured client checkout alone does not identify deployed code.

## Refresh the shared view

Each iteration has one Experimenter producing its `progress.yaml`; other authors keep analyses in their own files, and different iterations use separate records. Publish the complete record with the existing atomic writer so another owner's plot command cannot read a truncated file. JSON is valid YAML and works directly in the `.yaml` file. Run this after measurement has created the iteration's `notes/` directory, replacing the path with your actual iteration:

```bash
python - <<'PY'
from benchmarks.results.recording.files import write_json

write_json("results/my-experiment/iterations/qkv/notes", "progress.yaml", {
    "label": "+ QKV views",
    "measurements": {
        "random-concurrency-1": {
            "runs": ["../runs/perf-000001"],
            "parent": "../../baseline/runs/perf-000001",
            "status": "valid",
            "retained": True,
        },
    },
})
PY
```

After atomic replacement, any Experimenter can run the plot command above. Directory mode discovers and reads all owner records inside the existing publication lock, then refreshes the complete view; queued calls reread when they acquire the lock. This lock serializes aggregation and publication, not direct truncating writes by other tools.

Default charts show output throughput and mean TPOT. Select other saved metric fields with the existing options:

```bash
foretoken plot --progress results/my-experiment --metric ttft --stat p95
```

Metrics ending in `_seconds` use lower-is-better ratios; other selected metrics use higher-is-better ratios. Use an explicit chart YAML for a different objective. Custom metric selections refresh that view; use the same selection for owners sharing an output directory, or choose distinct `--output-dir` destinations. The original initial configuration remains the reference across resumptions. Continue new work from the applicable validated version and record the parent that actually ran.

## Existing chart YAML

Use the installed Foretoken CLI. Chinese labels need an installed Chinese font, such as Noto Sans CJK SC. From the repository root:

```bash
mkdir -p results/my-experiment/figures
cp rsi-playbooks/templates/progress-template.yaml results/my-experiment/figures/progress-main.yaml
```

In `progress-main.yaml`, fill in the title, comparison conditions and update time, then add the baseline and measurements as described below. Leave `baseline.value: null` until the baseline is measured. Generate the charts:

```bash
foretoken plot --progress results/my-experiment/figures/progress*.yaml
```

The command exports milestone and search charts as PNG, SVG and PDF, with names such as `progress-main-milestones.png` and `progress-main-search.png`. An unmeasured baseline produces pending-measurement charts. Use the [report template](report-template.md) to place the corresponding PNGs directly above their tables.

Use a separate file for each metric and comparison condition, such as `progress-concurrency-1-throughput.yaml` and `progress-concurrency-16-latency.yaml`. Each refresh replaces only the selected YAML stems; other conditions in the output directory remain available. Inputs in one directory default to that directory; inputs from different directories default to `./plots`. Use `--output-dir` to choose another destination. Each output name uses its input's filename stem. For a style preview, copy the [synthetic example](progress-example.yaml) to a temporary directory and render that copy.

## Record the results

The user's initial configuration is the only baseline. Keep one metric and comparable workload conditions per file; later controls belong in `references` and do not reset the baseline. Each point corresponds to one approach in the overview table, with the same name and summary value. Summarize repeated runs using the table's statistic and link the original measurements. Keep titles short, using the model, dataset and metric. Spell out concurrency levels, such as “Concurrency 1”. Detailed hardware, cache and source settings belong in the report's conditions and linked records.

| Field | Content |
| --- | --- |
| `title`, `subtitle` | Short title and optional caption. |
| `updated_at` | The report's update time, including timezone. |
| `x_label` | Meaning of `x`, such as elapsed hours, evaluation index or tool calls; the baseline is at zero. |
| `metric` | `name`, `unit` and `direction`: `higher` for throughput or `lower` for latency, for example. |
| `baseline` | The original initial-configuration measurement: `value`, `label` and evidence `source`. Leave `value: null` when that workload has no comparable initial measurement. |
| `points` | Each approach's `x`, `value`, `status` and `source`; optional `label` adds an annotation and `checkpoint: true` selects a cumulative milestone. |
| `phases` | Optional background bands with `label`, `start` and `end`, using the units of `x`. |
| `references` | Same-condition comparisons with `label` and `source`: use `value` for a horizontal line, or `points: [{x, value}, …]` for a measured trajectory in the search chart. |

Reference values use the same metric and baseline; trajectory points share the chart's `x` units. For example, `points: [{x: 0, value: 100}, {x: 2, value: 120}]` records two measurements, with evidence linked by the reference's `source`.

`source` is an evidence path relative to the YAML file, or a URL. Use the values summarized in the report and link their run records. Annotations are placed automatically; use `label_offset: [x, y]` for adjustments in typographic points.

| `status` | Meaning |
| --- | --- |
| `valid` | A comparable measured value that meets the task's constraints. |
| `incorrect` | Failed correctness or quality checks. |
| `compile_failed` | Build failed. |
| `runtime_failed` | Execution failed or produced no usable measurement. |

Failures appear in a separate lower strip. Set their `value: null` and omit `checkpoint` or set it to false. To invalidate an earlier result, make those changes and update its status, then regenerate the charts and link the correction in the iteration notes.

Follow the [validation guidance](../../.agents/skills/rsi/roles/common.md#review-claims-independently) to decide whether quality evaluation is needed. Comparable measurements can be `valid` when the change leaves output quality unaffected or existing evidence still applies. If an applicable quality question remains unresolved, retain the measured speed in the report and a named reference until it is resolved. References do not enter the best-so-far curve or milestones. For example:

```yaml
references:
  - label: Candidate speed; output consistency unresolved
    source: ../iterations/candidate/notes/analysis.md
    points: [{x: 3, value: 119}]
```

Classify results against the task's correctness and quality requirements, and record the retention decision separately. When review confirms a failure, record an `incorrect` attempt and explain it in the report; a reference can retain the observed speed with an updated label. Use separate comparisons for different hardware, workloads or measurement conditions.

## Read and maintain the charts

Improvement is `value / baseline` for `higher`, or `baseline / value` for `lower`. The original baseline is `1×`; larger ratios mean improvement. A local control is a named reference, not a new `1×` origin. Keep absolute values in the report when the original baseline has no matching measurement; `--progress` shows a pending-baseline chart in that case.

Milestones connect the baseline with valid points marked `checkpoint: true`. Each point represents a measured cumulative configuration, including a measured combination when several changes are retained together. Keep earlier selected versions in the history when a later version replaces them.

The search view includes improvements, regressions and failures, with a best-so-far curve starting at the named baseline. The report identifies retained versions. The `status` field controls plotting; ordinary points need no visible “valid” label.

On resumption, reuse the same data and baseline origin, including earlier adopted versions and failed attempts. Keep later control measurements as named references and retain existing points when an update contains no new measurements.
