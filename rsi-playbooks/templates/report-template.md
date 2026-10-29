# Optimization results

English | [简体中文](report-template_zh.md)

Use one continuing `report.md`: a visual summary followed by brief explanations of the approaches. Keep the task budget, execution logs, resource accounting and full configuration in their existing records.

## Summary

Place the matching milestone and search charts immediately above one overview table per task type. Put the original baseline first, keep concurrency levels side by side and show the signed change from that baseline beside each value. Use “—” for an unmeasured or incomparable value. Keep failed and rejected approaches visible.

<!-- Insert the generated retained-version and search figures from figures/progress here. -->

| Approach | Concurrency 1: throughput tokens/s ↑ | Concurrency 1: TPOT ms ↓ | Concurrency 16: throughput tokens/s ↑ | Concurrency 16: TPOT ms ↓ | Decision |
| --- | ---: | ---: | ---: | ---: | --- |
| baseline | value (0%) | value (0%) | value (0%) | value (0%) | Reference |
| [+ A](#a) | value (±%) | value (±%) | value (±%) | value (±%) | Retained / not retained / pending |
| [+ A + B](#a-and-b) | value (±%) | value (±%) | value (±%) | value (±%) | Retained / not retained / pending |

Names describe the complete configuration actually measured. A row and its chart points use the same label, order and summary statistic; show repeat count and variation when available. Local controls do not replace the original baseline. The existing `foretoken plot --progress` command owns the figures and generated numerical tables.

## Approaches

### A

Briefly explain the bottleneck, the implemented change and why it was retained, rejected or left pending. Name the actual parent and explain the incremental result where it changes the conclusion. Link the implementation and original experiment record; keep full measurements and logs there.

### A and B

Describe what B adds to the retained A configuration and what the measured combination established. Independent A and B experiments do not establish A+B. Continue successful changes on the best applicable usable configuration.
