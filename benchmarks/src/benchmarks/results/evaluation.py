# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Present native quality scores without recomputing or merging benchmark metrics."""

from __future__ import annotations

import csv
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import wandb

from benchmarks.config.evaluation import EvaluationConfig
from benchmarks.results.output import (
    BenchmarkRun,
    LocalDirectorySink,
    ResultSink,
    WandbSink,
    write_json,
)

logger = logging.getLogger(__name__)


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


def _display(row: dict[str, Any]) -> str:
    """Use upstream display units when provided, otherwise show the original value."""
    value = row["value"]
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return f"{value * row['display_multiplier']:.4g}{row['display_unit']}"
    return "—" if value is None else str(value)


def _execution_lines(execution: dict[str, Any]) -> list[str]:
    """Format native task completion counts for single evaluations and method comparisons."""
    return [
        f"{task}: {status.get('succeeded')} / {status.get('requested')} samples scored; {status.get('errored')} errors; incomplete={status.get('incomplete')}"
        for task, status in execution.items()
    ]


class EvaluationConsoleSink:
    """Print a readable task summary while keeping all detailed metrics in artifacts."""

    def open(self, record: dict[str, Any]) -> None:
        return None

    def publish(self, run: BenchmarkRun) -> None:
        """Print all task/group metrics with native filters and standard errors."""
        rows = run.metrics["scores"]
        visible = [row for row in rows if row["level"] in ("task", "group")]
        table = [["Task", "Metric", "Filter", "Samples", "Value", "Std. error"]]
        for row in visible:
            table.append(
                [
                    row["task"],
                    row["metric"],
                    row["filter"] or "—",
                    str(row["samples"]) if row["samples"] is not None else "—",
                    _display(row),
                    str(row["stderr"]) if row["stderr"] is not None else "—",
                ]
            )
        widths = [max(len(line[i]) for line in table) for i in range(len(table[0]))]
        status = "failed" if run.exit_code else "completed"
        if not run.exit_code and any(
            item.get("incomplete") for item in run.metrics["execution"].values()
        ):
            status = "completed with incomplete results"
        lines = [
            "Foretoken evaluation",
            f"Model: {run.record['model']}    Evaluator: {run.record['evaluator']}",
            f"Status: {status}    Duration: {run.metrics['duration_seconds']:.1f}s",
            "",
        ]
        for line in table:
            lines.append(
                "  ".join(value.ljust(width) for value, width in zip(line, widths))
            )
        if not visible:
            lines.append("No scored tasks. See evaluator.log and native outputs.")
        if any(row["level"] in ("category", "subset") for row in rows):
            lines.append(
                "Category and subset scores are included in metrics.json and W&B Scores."
            )
        lines.extend(_execution_lines(run.metrics["execution"]))
        logger.info("\n%s", "\n".join(lines))

    def close(self, *, exit_code: int = 0) -> None:
        return None


class EvaluationArtifactSink:
    """Save evaluation invocation identity beside native artifacts and shared metrics."""

    def __init__(self, config: EvaluationConfig, directory: str) -> None:
        self.config = config
        self.directory = directory

    def open(self, record: dict[str, Any]) -> None:
        write_json(self.directory, "config.json", {**self.config.to_dict(), **record})

    def publish(self, run: BenchmarkRun) -> None:
        run.artifacts["config"] = Path(self.directory) / "config.json"

    def close(self, *, exit_code: int = 0) -> None:
        return None


def publish_quality_wandb(session: Any, run: BenchmarkRun) -> None:
    """Publish native-valued scalars, a complete score table, and downloadable run artifacts."""
    columns = [
        *(["method"] if run.record.get("mode") == "evaluation_comparison" else []),
        "task",
        "level",
        "subset",
        "filter",
        "metric",
        "value",
        "stderr",
        "samples",
        "direction",
        "primary",
    ]
    rows = run.metrics["scores"]
    session.log(
        {
            "Evaluation/Scores": wandb.Table(
                columns=columns,
                data=[[row[key] for key in columns] for row in rows],
                allow_mixed_types=True,
            )
        }
    )
    summary = {
        "Evaluation/Exit code": run.exit_code,
        "Evaluation/Duration (s)": run.metrics["duration_seconds"],
    }
    for row in rows:
        if isinstance(row["value"], (int, float)) and not isinstance(
            row["value"], bool
        ):
            key = "/".join(
                quote(str(row[k]), safe="") or "all"
                for k in (("method",) if "method" in row else ()) + ("task", "level", "subset", "filter", "metric")
            )
            summary[f"Evaluation/{key}"] = row["value"]
    summary["Evaluation/Execution"] = run.metrics["execution"]
    session.summary.update(summary)
    artifact = wandb.Artifact(f"evaluation-{session.id}", type="evaluation")
    for name, path in run.artifacts.items():
        if name == "plots":
            continue
        if path.is_dir():
            artifact.add_dir(str(path), name=name)
        else:
            artifact.add_file(str(path), name=path.name)
    session.log_artifact(artifact)
    if session.url:
        logger.info("W&B results: %s", session.url)


class EvaluationComparisonArtifactSink:
    """Save aligned multi-method scores as a spreadsheet beside child evaluator outputs."""

    def __init__(self, directory: str) -> None:
        self.directory = Path(directory)

    def open(self, record: dict[str, Any]) -> None:
        return None

    def publish(self, run: BenchmarkRun) -> None:
        columns = ("method", "task", "level", "subset", "filter", "metric", "value",
                   "stderr", "samples", "direction", "display_multiplier", "display_unit", "primary")
        path = self.directory / "evaluation_comparison.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(run.metrics["scores"])
        run.artifacts["evaluation_comparison_csv"] = path

    def close(self, *, exit_code: int = 0) -> None:
        return None


class EvaluationComparisonConsoleSink:
    """Show aligned task scores for each evaluated deployment."""

    def open(self, record: dict[str, Any]) -> None:
        return None

    def publish(self, run: BenchmarkRun) -> None:
        for method in run.metrics["evaluation_comparison"]["methods"]:
            native = run.metrics["execution"]["methods"].get(method["label"], {})
            status = method["status"]
            if status == "completed" and any(task.get("incomplete") for task in native.values()):
                status = "completed with incomplete results"
            logger.info("Method %s (%s): %s", method["label"], method["model"], status)
            for line in _execution_lines(native):
                logger.info("  %s", line)
        for row in run.metrics["scores"]:
            if row["level"] == "task":
                logger.info("%s · %s · %s/%s: %s", row["method"], row["task"],
                            row["metric"], row["filter"], _display(row))

    def close(self, *, exit_code: int = 0) -> None:
        return None


def publish_evaluation_comparison_wandb(session: Any, run: BenchmarkRun) -> None:
    """Publish matched task scores and child artifacts through the quality W&B sink."""
    publish_quality_wandb(session, run)
    from benchmarks.results.plots.measurements import _evaluation_comparison_charts

    for chart in _evaluation_comparison_charts(run.metrics["evaluation_comparison"]):
        table = wandb.Table(columns=["Method", chart.ylabel], data=[
            [series.name, series.y[0]] for series in chart.series
        ])
        session.log({f"Evaluation Comparison/{chart.name}": wandb.plot.bar(
            table, "Method", chart.ylabel, title=chart.title,
        )})


def evaluation_comparison_sinks(config: EvaluationConfig, record: dict[str, Any], directory: str) -> list[ResultSink]:
    """Compose one quality comparison around the existing result destinations."""
    sinks = evaluation_sinks(
        config, record, directory,
        console_sink=EvaluationComparisonConsoleSink(),
        publisher=publish_evaluation_comparison_wandb,
    )
    return [sinks[0], EvaluationComparisonArtifactSink(directory), *sinks[1:]]


def evaluation_sinks(
    config: EvaluationConfig, record: dict[str, Any], directory: str,
    *,
    console_sink: ResultSink | None = None,
    publisher: Callable[[Any, BenchmarkRun], None] = publish_quality_wandb,
) -> list[ResultSink]:
    """Use the existing publication lifecycle with evaluation-specific presentation."""
    sinks: list[ResultSink] = [EvaluationArtifactSink(config, directory)]
    sinks.append(console_sink if console_sink is not None else EvaluationConsoleSink())
    if config.outputs.saves_local:
        sinks.append(LocalDirectorySink(directory))
    if config.outputs.includes("wandb"):
        sinks.append(
            WandbSink(
                config,
                execution_dir=directory,
                run_name=config.wandb.run_name
                or "_".join(str(part) for part in (
                    record.get("model"), record.get("evaluator"), Path(directory).name,
                ) if part),
                group=config.wandb.group,
                publisher=publisher,
                run_config={**config.to_dict(), **record},
            )
        )
    return sinks
