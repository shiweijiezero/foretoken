# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Adapt duration-qualified SLO thresholds to EvalScope's comparison rules."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from evalscope.perf.sla.sla_criterion import SLACriterionBase


def parse_slo_criteria(params: list[dict[str, str]]) -> list[dict[str, SLACriterionBase]]:
    """Return second-based rules for validation, scoring, searches and saved-result plots.

    EvalScope owns comparison syntax. Duration suffixes only scale latency
    targets; unitless values retain their native meaning.
    """
    from evalscope.perf.sla.sla_run import parse_sla_params

    normalized = []
    scales = []
    for group in params:
        expressions = {}
        factors = {}
        for metric, expression in group.items():
            expression = expression.strip()
            for suffix, factor in (("ms", 0.001), ("s", 1.0)):
                if expression.endswith(suffix):
                    if metric.split(".", 1)[0].rsplit("_", 1)[-1] not in {"latency", "ttft", "tpot", "itl"}:
                        raise ValueError(f"Time units are not supported for SLO metric {metric!r}")
                    expression = expression[:-len(suffix)]
                    if expression.strip() in {"min", "max"}:
                        raise ValueError("SLO duration units require a numeric threshold")
                    factors[metric] = factor
                    break
            expressions[metric] = expression
        normalized.append(expressions)
        scales.append(factors)

    rules = parse_sla_params(normalized)
    for group, factors in zip(rules, scales):
        for metric, factor in factors.items():
            group[metric].target *= factor
    return rules
