<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Plot RSI optimization progress

English | [简体中文](progress-charts_zh.md)

The milestone chart follows measured cumulative versions. The search chart shows the compared approaches, the best result so far and failed attempts. Place the matching search chart immediately above each overview table in the [experiment report](../experiment-records.md#maintain-the-user-facing-report).

## Create the charts

Use the installed Foretoken CLI. Chinese labels need an installed Chinese font, such as Noto Sans CJK SC. From the repository root:

```bash
mkdir -p results/my-experiment/figures
cp rsi-playbooks/templates/progress-template.yaml results/my-experiment/figures/progress.yaml
```

In `progress.yaml`, fill in the title, comparison conditions and update time, then add the baseline and measurements as described below. Leave `baseline.value: null` until the baseline is measured. Generate the charts:

```bash
foretoken plot --progress results/my-experiment/figures/progress*.yaml
```

The command exports milestone and search charts as PNG, SVG and PDF, with names such as `progress-milestones.png` and `progress-search.png`. An unmeasured baseline produces pending-measurement charts. Use the [report template](report-template.md) to place the corresponding PNGs directly above their tables.

Use a separate file for each metric and comparison condition, such as `progress-concurrency-1-throughput.yaml` and `progress-concurrency-16-latency.yaml`. Export all of the report's conditions together: each invocation replaces the previous chart files in its output directory. Inputs in one directory default to that directory; inputs from different directories default to `./plots`. Use `--output-dir` to choose another destination. Each output name uses its input's filename stem. For a style preview, copy the [synthetic example](progress-example.yaml) to a temporary directory and render that copy.

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
