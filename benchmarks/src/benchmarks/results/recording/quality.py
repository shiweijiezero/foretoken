# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Decode recorded evaluator conditions and scores without invoking an evaluator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_quality_conditions(evaluator: str, directory: Path) -> dict[str, Any] | None:
    """Read recorded task choices for saved-result grouping without loading an evaluator or changing its reports."""
    generation_fields = (
        "max_tokens", "max_new_tokens", "max_gen_toks", "temperature", "top_p", "top_k",
        "do_sample", "n", "seed", "stop", "stop_seqs", "until", "repetition_penalty",
        "frequency_penalty", "presence_penalty", "reasoning_effort", "reasoning_tokens",
    )
    if evaluator == "lm-eval":
        reports = []
        for path in sorted(directory.glob("**/results_*.json")):
            report = json.loads(path.read_text(encoding="utf-8"))
            config = report.get("config", {})
            reports.append({
                **{key: config[key] for key in (
                    "limit", "random_seed", "numpy_seed", "torch_seed", "fewshot_seed",
                ) if key in config},
                **{key: report[key] for key in (
                    "system_instruction", "fewshot_as_multiturn", "chat_template",
                ) if key in report},
                "model_options": {key: config.get("model_args", {})[key] for key in (
                    "tokenizer", "tokenizer_backend", "revision", "max_length", "max_gen_toks",
                    "seed", "truncate", "eos_string",
                ) if key in config.get("model_args", {})},
                "tasks": {name: {
                    **{key: task[key] for key in (
                        "dataset_path", "dataset_name", "test_split", "validation_split",
                        "fewshot_split", "output_type", "repeats",
                    ) if key in task},
                    "num_fewshot": report.get("n-shot", {}).get(name),
                    "generation": {key: task["generation_kwargs"][key] for key in generation_fields
                                   if key in (task.get("generation_kwargs") or {})},
                    "prompt_format": (task.get("metadata") or {}).get("foretoken", {}).get("prompt_format"),
                    "version": report.get("versions", {}).get(name),
                } for name, task in report.get("configs", {}).items()},
            })
        return {"reports": reports} if reports else None
    path = directory / "configs/task_config.yaml"
    if not path.is_file():
        return None
    import yaml

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


def read_quality_metrics(evaluator: str, directory: Path) -> dict[str, Any]:
    """Read every native task report; retain subset, filter, sample count, and uncertainty."""
    rows: list[dict[str, Any]] = []
    execution: dict[str, Any] = {}
    pattern = "**/results_*.json" if evaluator == "lm-eval" else "reports/**/*.json"
    for path in sorted(directory.glob(pattern)):
        result = json.loads(path.read_text(encoding="utf-8"))
        if evaluator == "lm-eval":
            for level, key in (("task", "results"), ("group", "groups")):
                for task, scores in result.get(key, {}).items():
                    counts = result.get("n-samples", {}).get(task, {})
                    for name, value in scores.items():
                        # Harness metric keys include the filter; descriptive fields
                        # such as alias and sample_len are not measurements.
                        if "," not in name:
                            continue
                        metric, filter_name = name.split(",", 1)
                        if metric.endswith("_stderr"):
                            continue
                        rows.append(
                            {
                                "task": task,
                                "level": level,
                                "subset": "",
                                "filter": filter_name,
                                "metric": metric,
                                "value": value,
                                "stderr": (
                                    None
                                    if scores.get(f"{metric}_stderr,{filter_name}")
                                    == "N/A"
                                    else scores.get(f"{metric}_stderr,{filter_name}")
                                ),
                                "samples": counts.get(
                                    "effective", scores.get("sample_len")
                                ),
                                "direction": result.get("higher_is_better", {})
                                .get(task, {})
                                .get(metric),
                                "display_multiplier": 1,
                                "display_unit": "",
                                "primary": None,
                            }
                        )
        else:
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
