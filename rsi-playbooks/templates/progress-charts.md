<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Plot RSI optimization progress

English | [简体中文](progress-charts_zh.md)

The milestone chart follows measured cumulative versions. The search chart shows all valid attempts, the best result so far and failed attempts. Both accompany the [experiment report](../experiment-records.md#maintain-the-user-facing-report).

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

The command exports milestone and search charts as PNG, SVG and PDF, with names such as `progress-milestones.png` and `progress-search.png`. An unmeasured baseline produces pending-measurement charts. Insert the PNGs after the report's results tables using the [report template](report-template.md).

Use a separate file for each metric and comparison condition, such as `progress-c1-throughput.yaml` and `progress-c16-latency.yaml`. Export all of the report's conditions together: each invocation replaces the previous chart files in its output directory. Inputs in one directory default to that directory; inputs from different directories default to `./plots`. Use `--output-dir` to choose another destination. Each output name uses its input's filename stem. For a style preview, copy the [synthetic example](progress-example.yaml) to a temporary directory and render that copy.

## Record the results

Keep one metric, baseline and set of conditions per file. Describe the model, hardware, precision, workload, concurrency and measurement scope in `subtitle`.

| Field | Content |
| --- | --- |
| `title`, `subtitle` | Title and comparison conditions; the title receives a `Foretoken · ` prefix. |
| `updated_at` | The report's update time, including timezone. |
| `x_label` | Meaning of `x`, such as elapsed hours, evaluation index or tool calls; the baseline is at zero. |
| `metric` | `name`, `unit` and `direction`: `higher` for throughput or `lower` for latency, for example. |
| `baseline` | Measured `value`, `label` and evidence `source`; leave `value: null` until measured. |
| `points` | Each attempt's `x`, `value`, `status` and `source`; optional `label` adds an annotation and `checkpoint: true` selects a cumulative milestone. |
| `phases` | Optional background bands with `label`, `start` and `end`, using the units of `x`. |
| `references` | Same-condition comparisons with `label` and `source`: use `value` for a horizontal line, or `points: [{x, value}, …]` for a measured trajectory in the search chart. |

Reference values use the same metric and baseline; trajectory points share the chart's `x` units. For example, `points: [{x: 0, value: 100}, {x: 2, value: 120}]` records two measurements, with evidence linked by the reference's `source`.

`source` is an evidence path relative to the YAML file, or a URL. Use the values summarized in the report and link their run records. Annotations are placed automatically; use `label_offset: [x, y]` for adjustments in typographic points.

| `status` | Meaning |
| --- | --- |
| `valid` | A comparable measured value that passed the required checks. |
| `incorrect` | Failed correctness or quality checks. |
| `compile_failed` | Build failed. |
| `runtime_failed` | Execution failed or produced no usable measurement. |

Failures appear in a separate lower strip. Set their `value: null` and omit `checkpoint` or set it to false. To invalidate an earlier result, make those changes and update its status, then regenerate the charts and link the correction in the iteration notes.

## Read and maintain the charts

Improvement is `value / baseline` for `higher`, or `baseline / value` for `lower`. The baseline is `1×`; larger ratios mean improvement.

Milestones connect the baseline with valid points marked `checkpoint: true`. Each point represents a measured cumulative configuration, including a measured combination when several changes are retained together. Keep earlier selected versions in the history when a later version replaces them.

The search view plots all valid values and a best-so-far step curve starting at the baseline. The report identifies the currently retained version.

On resumption, reuse the same data and baseline origin. Keep existing points when an update contains no new measurements.
