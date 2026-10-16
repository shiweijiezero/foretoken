<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 05 | Recording and Learning

English | [简体中文](05-recording-and-learning_zh.md)

Turn research findings, failure causes, and unproductive detours into reusable knowledge. Preserve the evidence and entry points needed to continue investigating so subsequent tasks can build on existing progress.

## Extract knowledge from experiments

Raw artifacts preserve commands, configurations, versions, metrics, and outputs. Iteration notes explain the question, changes, results, and next steps. See the [experiment records guide](../../experiment-records.md) for recording instructions.

Link knowledge entries to the original analysis and evidence, and agree on one writer per entry.

Extract useful insights from reference reading, source analysis, and experiments:

- Effective improvements: why they work and which models, workloads, hardware, and implementation conditions they suit.
- Unsuccessful attempts: what happened, which causes were identified, and what changes might justify another attempt.
- New findings: implementation behavior, technical trade-offs, corrections to earlier understanding, and questions for further exploration.
- Working practices: which practices improved efficiency and which caused repeated investigation, uninformative measurements, repeated builds, or waiting. Record the causes, early warning signs, and concrete practices to use next time.

Explain causes around a specific question, with evidence and applicable conditions. For example, if a change works for long inputs at high concurrency, explain how the relevant amount of work changes. If a backend lacks operator support, record the missing capability and call path. Turn inefficient processes into actionable lessons: if repeated searches resulted from not reading historical records, identify the entries to check first; if repeated full builds resulted from overlooking an incremental workflow, preserve the corresponding framework procedure.

## Preserve usable entry points

Knowledge entries explain reusable input construction, algorithms, and implementation conditions, linking stable code locations and supporting experiments. Explain how shapes, layouts, and state affect the mechanism so readers can reproduce or adapt it.

Keep complete run commands, experiment scripts, validation progress, and remaining work in the experiment records. Technical entries describe mechanisms and established effects rather than task-specific execution plans.

- Put technical principles, implementation experience, and optimization findings in [technical knowledge](../knowledge/README.md).
- Put search, analysis, decision-making, design, and learning methods in [meta-knowledge](README.md).
- Keep each iteration's configurations, logs, and raw results in the experiment directory.

Use a title that expresses the complete goal and method, and place the note in its category directory. Update the knowledge index when adding a category so readers can find it.

## Knowledge note template

Use the [knowledge note template](../../templates/guidance-template.md): observations and motivation, principles and analysis, design, implementation, impact and results, and references. Start from the observed behavior, explain its causes and the method's mechanism, then connect implementation choices to their effects. Use a short target object and core technique for the file name, not a full sentence; let the title express the complete goal and method.

## Update and use the knowledge in subsequent research

Search for existing entries before writing, and extend or correct an existing note on the same topic where possible. When new results change the understanding, update conclusions, applicable conditions, and evidence. Explain results under different conditions side by side. After version changes, check whether code and operational entry points still apply.

Read relevant knowledge and experiments before deciding whether to reuse an approach, adjust its implementation, or add measurements. Cite entries that influenced the design in iteration notes and feed new findings back into them. Accumulating technical understanding, usable implementations, and experiment methods together reduces repeated investigation and preparation over time.
