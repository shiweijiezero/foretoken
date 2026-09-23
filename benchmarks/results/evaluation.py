# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Present native quality scores without recomputing or merging benchmark metrics."""

from __future__ import annotations

import json
import logging
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
        for task, status in run.metrics["execution"].items():
            lines.append(
                f"{task}: {status.get('succeeded')} / {status.get('requested')} samples scored; {status.get('errored')} errors; incomplete={status.get('incomplete')}"
            )
        logger.info("\n%s", "\n".join(lines))

    def close(self, *, exit_code: int = 0) -> None:
        return None


class EvaluationArtifactSink:
    """Save invocation identity and the complete display projection beside native artifacts."""

    def __init__(self, config: EvaluationConfig, directory: str) -> None:
        self.config = config
        self.directory = directory

    def open(self, record: dict[str, Any]) -> None:
        write_json(self.directory, "config.json", {**self.config.to_dict(), **record})

    def publish(self, run: BenchmarkRun) -> None:
        run.artifacts["metrics"] = write_json(
            self.directory, "metrics.json", run.metrics
        )
        run.artifacts["config"] = Path(self.directory) / "config.json"

    def close(self, *, exit_code: int = 0) -> None:
        return None


def publish_quality_wandb(session: Any, run: BenchmarkRun) -> None:
    """Publish native-valued scalars, a complete score table, and downloadable run artifacts."""
    columns = [
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
                for k in ("task", "level", "subset", "filter", "metric")
            )
            summary[f"Evaluation/{key}"] = row["value"]
    summary["Evaluation/Execution"] = run.metrics["execution"]
    session.summary.update(summary)
    artifact = wandb.Artifact(f"evaluation-{session.id}", type="evaluation")
    for name, path in run.artifacts.items():
        if path.is_dir():
            artifact.add_dir(str(path), name=name)
        else:
            artifact.add_file(str(path), name=path.name)
    session.log_artifact(artifact)
    if session.url:
        logger.info("W&B results: %s", session.url)


def evaluation_sinks(
    config: EvaluationConfig, record: dict[str, Any], directory: str
) -> list[ResultSink]:
    """Use the existing publication lifecycle with evaluation-specific presentation."""
    sinks: list[ResultSink] = [EvaluationArtifactSink(config, directory)]
    if not config.outputs.includes("quiet"):
        sinks.append(EvaluationConsoleSink())
    if config.outputs.includes("local"):
        sinks.append(LocalDirectorySink(directory))
    if config.outputs.includes("wandb"):
        sinks.append(
            WandbSink(
                config,
                execution_dir=directory,
                run_name=config.wandb.run_name
                or f"{record['model']}_{record['evaluator']}_{Path(directory).name}",
                group=config.wandb.group,
                publisher=publish_quality_wandb,
                run_config={**config.to_dict(), **record},
            )
        )
    return sinks
