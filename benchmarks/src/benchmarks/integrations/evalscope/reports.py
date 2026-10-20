# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Decode saved EvalScope conditions and scores without loading the evaluation framework."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_conditions(directory: Path) -> dict[str, Any] | None:
    """Read native task choices for saved-result grouping without changing the EvalScope reports."""
    path = directory / "configs/task_config.yaml"
    if not path.is_file():
        return None
    import yaml

    generation_fields = (
        "max_tokens", "max_new_tokens", "max_gen_toks", "temperature", "top_p", "top_k",
        "do_sample", "n", "seed", "stop", "stop_seqs", "until", "repetition_penalty",
        "frequency_penalty", "presence_penalty", "reasoning_effort", "reasoning_tokens",
    )
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {
        **{key: config[key] for key in (
            "datasets", "limit", "seed", "repeats", "dataset_hub", "chat_template",
        ) if key in config},
        "generation": {key: config["generation_config"][key] for key in generation_fields
                       if key in (config.get("generation_config") or {})},
        "judge": {
            **{key: config["judge"][key] for key in (
                "strategy", "repeats", "aggregation",
            ) if key in (config.get("judge") or {})},
            "models": [model["model_id"] for model in (config.get("judge") or {}).get("models", [])],
        },
        "tasks": {name: {
            **{key: task[key] for key in (
                "dataset_id", "dataset_revision", "subset_list", "few_shot_num", "few_shot_random",
                "train_split", "eval_split", "shuffle", "shuffle_choices", "prompt_template",
                "system_prompt", "few_shot_prompt_template", "query_template",
            ) if key in task},
            "evaluation_version": config.get("evaluation_identity", {}).get("benchmarks", {}).get(name, {}).get("evaluation_version"),
        } for name, task in config.get("resolved_benchmarks", config.get("dataset_args", {})).items()},
    }


def read_metrics(directory: Path) -> dict[str, Any]:
    """Normalize native task reports for publication, retaining categories, subsets and execution status."""
    rows: list[dict[str, Any]] = []
    execution: dict[str, Any] = {}
    for path in sorted(directory.glob("reports/**/*.json")):
        result = json.loads(path.read_text(encoding="utf-8"))
        if result.get("execution_summary"):
            execution[result["dataset_name"]] = result["execution_summary"]
        for metric in result["metrics"]:
            identity = metric["identity"]
            dimensions = identity.get("dimensions") or {}
            name = identity["name"]
            if dimensions:
                name += " " + json.dumps(
                    dimensions, sort_keys=True, ensure_ascii=False
                )
            semantics = metric.get("semantics") or {}
            base = {
                "task": result["dataset_name"],
                "metric": name,
                "filter": identity.get("aggregation", ""),
                "stderr": None,
                "direction": semantics.get("direction"),
                "display_multiplier": semantics.get("display_multiplier") or 1,
                "display_unit": semantics.get("display_unit") or "",
                "primary": identity == result.get("primary_metric_identity"),
            }
            rows.append(
                {
                    **base,
                    "level": "task",
                    "subset": "",
                    "value": metric["score"],
                    "samples": metric.get("num"),
                }
            )
            if metric.get("macro_score") is not None:
                rows.append(
                    {
                        **base,
                        "level": "task",
                        "subset": "",
                        "metric": name + " (macro)",
                        "value": metric["macro_score"],
                        "samples": metric.get("num"),
                    }
                )
            for category in metric.get("categories", []):
                category_name = "/".join(category["name"])
                rows.append(
                    {
                        **base,
                        "level": "category",
                        "subset": category_name,
                        "value": category["score"],
                        "samples": category.get("num"),
                    }
                )
                for subset in category.get("subsets", []):
                    rows.append(
                        {
                            **base,
                            "level": "subset",
                            "subset": f"{category_name}/{subset['name']}",
                            "value": subset["score"],
                            "samples": subset.get("num"),
                        }
                    )
    return {"scores": rows, "execution": execution}
