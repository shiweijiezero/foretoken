# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Decode saved harness conditions and scores without loading the evaluation framework."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_conditions(directory: Path) -> dict[str, Any] | None:
    """Read native task choices for saved-result grouping without changing the harness reports."""
    generation_fields = (
        "max_tokens", "max_new_tokens", "max_gen_toks", "temperature", "top_p", "top_k",
        "do_sample", "n", "seed", "stop", "stop_seqs", "until", "repetition_penalty",
        "frequency_penalty", "presence_penalty", "reasoning_effort", "reasoning_tokens",
    )
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


def read_metrics(directory: Path) -> dict[str, Any]:
    """Normalize every native task and group score for result publication, retaining filters and uncertainty."""
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("**/results_*.json")):
        result = json.loads(path.read_text(encoding="utf-8"))
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
    return {"scores": rows, "execution": {}}
