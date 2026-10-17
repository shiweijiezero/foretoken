# Goal and Technique

English | [简体中文](guidance-template_zh.md)

Use a short target object and core technique for the file name, not a full sentence; let the title express the complete goal and method. Place technical entries in the appropriate [knowledge category](../guidance/knowledge/README.md) and organize the following content around the topic.

## Observations and Motivation

Start with the observed behavior, the conditions that produce it, and its impact, such as changes in latency, throughput, memory traffic, or quality under a specific workload. Explain where the existing approach encounters a bottleneck or limitation and what should improve.

## Principles and Analysis

Explain the mechanism and its causal relationships through examples, equations, or execution steps. Identify the computation, memory traffic, communication, or waiting it saves and the costs it adds. Explain how shapes, workloads, or hardware change the trade-off, using comparisons to distinguish competing explanations.

## Design

Describe how the approach organizes computation, data, state, and coordination. Explain the important choices and trade-offs through one complete execution, showing how the parts work together and why they are arranged that way.

## Implementation

Explain the core implementation through concise steps, an algorithm, or pseudocode: inputs, key computation or state updates, and outputs. Add code entry points and the model, hardware, precision, and version conditions that affect use. For methods requiring training or new weights, explain how to obtain and prepare them.

## Impact and Results

When reliable measurements are available, give concrete values, the comparison, and the relevant workload, hardware, and precision, linking the original results. Otherwise explain the bottleneck addressed, work reduced, and costs introduced. Attribute the effect to the responsible change. Describe established effects here rather than listing proposed experiments.

## References

Link original papers, implementations, and relevant experiments. Version-sensitive statements point to the corresponding paper version, release, or source location.
