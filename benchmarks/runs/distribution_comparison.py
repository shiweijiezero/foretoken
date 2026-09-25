# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Compare served models on shared teacher-forced prefixes and publish their distribution differences."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Iterator

import httpx
import numpy as np

from foretoken.manifest import DeploymentError

from benchmarks.config.benchmark import ModelServiceSource
from benchmarks.config.evaluation import EvaluationConfig
from benchmarks.config.distribution_comparison import DistributionComparisonConfig
from benchmarks.datasets.conversations import iter_jsonl_rows
from benchmarks.datasets.huggingface import resolve_tokenizer_path
from benchmarks.integrations.distributions import CompletionDistributionClient, compare_logprobs
from benchmarks.model_service import ModelService, resolve_model_service
from benchmarks.results.environment import serving_environment
from benchmarks.results.distribution_comparison import distribution_comparison_sinks
from benchmarks.results.distribution_comparison_checkpoint import DistributionComparisonCheckpoint
from benchmarks.results.output import BenchmarkRun, ResultOutputs, write_json

logger = logging.getLogger(__name__)


def _text_rows(config: DistributionComparisonConfig) -> Iterator[str]:
    """Read the selected public corpus or local text without interpreting it as chat turns."""
    path = Path(config.dataset).expanduser()
    if path.is_file() and path.suffix != ".jsonl":
        with path.open(encoding="utf-8") as source:
            yield from source
        return
    if path.is_file():
        rows = (row for _, _, _, row in iter_jsonl_rows(path))
    else:
        from datasets import load_dataset

        rows = iter(load_dataset(
            config.dataset, name=config.dataset_config, split=config.split, streaming=True
        ))
    for row in rows:
        if not isinstance(row, dict) or config.text_column not in row:
            raise ValueError(f"Comparison dataset rows require text column {config.text_column!r}")
        value = row[config.text_column]
        if not isinstance(value, str):
            raise ValueError(f"Comparison text column {config.text_column!r} must contain strings")
        if value.strip():
            yield value + "\n"


def _token_windows(config: DistributionComparisonConfig, tokenizer: Any) -> list[list[int]]:
    """Take complete, non-overlapping windows, adding the tokenizer's BOS per window when defined."""
    bos = [] if tokenizer.bos_token_id is None else [tokenizer.bos_token_id]
    buffer = list(bos)
    windows = []
    for text in _text_rows(config):
        buffer.extend(tokenizer.encode(text, add_special_tokens=False))
        while len(buffer) >= config.context_length:
            windows.append(buffer[:config.context_length])
            if len(windows) == config.num_windows:
                return windows
            buffer = bos + buffer[config.context_length:]
    raise ValueError(
        f"Dataset supplies {len(windows)} complete windows; requested {config.num_windows}. "
        "Reduce --num-windows or --context-length, or provide more text."
    )


def _candidate_summary(rows: list[dict[str, Any]], top_k: tuple[int, ...]) -> dict[str, Any]:
    """Aggregate equally weighted scored positions without averaging per-window means."""
    kl = np.asarray([row["kl"] for row in rows], dtype=np.float64)
    delta = np.asarray([row["corpus_token_delta_p"] for row in rows], dtype=np.float64)
    return {
        "scored_positions": len(rows),
        "mean_kl": float(kl.mean()),
        "median_kl": float(np.median(kl)),
        "p99_kl": float(np.percentile(kl, 99, method="inverted_cdf")),
        "top1_agreement": float(np.mean([row["top1_match"] for row in rows])),
        **{f"top{k}_overlap": float(np.mean([row[f"top{k}_overlap"] for row in rows])) for k in top_k},
        "corpus_token_mean_delta_p": float(delta.mean()),
        "corpus_token_rms_delta_p": float(np.sqrt(np.mean(delta ** 2))),
        "mean_centered_logit_rmse": float(np.mean([row["centered_logit_rmse"] for row in rows])),
        "mean_total_variation": float(np.mean([row["total_variation"] for row in rows])),
    }


def _score_rows(points: list[dict[str, Any]], top_k: tuple[int, ...]) -> list[dict[str, Any]]:
    """Project comparison metrics onto the existing quality result table."""
    higher = {"top1_agreement", *(f"top{k}_overlap" for k in top_k)}
    names = [
        "mean_kl", "median_kl", "p99_kl", "top1_agreement",
        *(f"top{k}_overlap" for k in top_k),
        "corpus_token_mean_delta_p", "corpus_token_rms_delta_p",
        "mean_centered_logit_rmse", "mean_total_variation",
    ]
    return [{
        "task": point["label"], "level": "task", "subset": "", "filter": "teacher_forced",
        "metric": name, "value": point[name], "stderr": None,
        "samples": point["scored_positions"],
        "direction": "higher" if name in higher else (None if name == "corpus_token_mean_delta_p" else "lower"),
        "display_multiplier": 100 if name in higher else 1,
        "display_unit": "%" if name in higher else "",
        "primary": name == "mean_kl",
    } for point in points for name in names]


def _reference_text_files(service: ModelService, override: str) -> tuple[str, str, str]:
    """Resolve the reference's tokenizer and output-head config independently on the client."""
    source, tokenizer_id = "hf", service.model
    if service.deployment is not None:
        identities = {
            (spec.get("source", "hf"), spec.get("tokenizer") or spec["model"])
            for document in service.deployment.objects
            if document["kind"] == "ModelService"
            and (spec := document["spec"])["model"] == service.model
        }
        if len(identities) != 1:
            raise ValueError("The reference deployment must select one model/tokenizer identity")
        source, tokenizer_id = identities.pop()
    if override:
        path = resolve_tokenizer_path(override, source="hf" if source == "local" else source)
        return path, path, override
    try:
        model_path = resolve_tokenizer_path(service.model, source=source)
        tokenizer_path = (
            model_path if tokenizer_id == service.model
            else resolve_tokenizer_path(tokenizer_id, source=source)
        )
    except (OSError, ValueError) as error:
        raise ValueError(
            "Cannot load the reference model's text artifacts on this client. "
            "Use --tokenizer-path with its base-model repository or a local directory "
            "containing tokenizer files and config.json."
        ) from error
    return tokenizer_path, model_path, tokenizer_id


def _score_reference(
    source: ModelServiceSource,
    comparison: DistributionComparisonConfig,
    checkpoint: DistributionComparisonCheckpoint,
) -> dict[str, Any]:
    """Prepare fixed corpus windows once and complete their reference probabilities before candidates."""
    from transformers import AutoConfig, AutoTokenizer

    saved = checkpoint.get("reference")
    completed = checkpoint.reference_done()
    if len(completed) == comparison.num_windows:
        logger.info("Reusing all %d reference windows", len(completed))
        return saved
    with resolve_model_service(source) as reference:
        if saved is None:
            tokenizer_path, model_path, tokenizer_id = _reference_text_files(reference, comparison.tokenizer)
            tokenizer = AutoTokenizer.from_pretrained(tokenizer_path)
            # The output head may contain padding beyond the tokenizer vocabulary.
            model_config = AutoConfig.from_pretrained(model_path).to_dict()
            vocabulary_size = model_config.get("text_config", model_config)["vocab_size"]
            if max(tokenizer.get_vocab().values()) >= vocabulary_size:
                raise ValueError("Tokenizer token IDs exceed the model configuration's vocabulary size")
            saved = {
                "model": reference.model,
                "windows": _token_windows(comparison, tokenizer),
                "protocol": {
                    **comparison.protocol(), "tokenizer": tokenizer_id,
                    "bos_token_id": tokenizer.bos_token_id,
                    "tokenizer_vocab_size": len(tokenizer), "model_vocab_size": vocabulary_size,
                },
                "environment": serving_environment(reference),
            }
            checkpoint.put("reference", saved)
        elif reference.model != saved["model"]:
            raise ValueError("Reference model differs from the saved comparison")
        vocabulary_size = saved["protocol"]["model_vocab_size"]
        score_positions = range(comparison.context_length - comparison.score_tokens, comparison.context_length)
        logger.info("Reference: %s | %d/%d windows already complete", reference.model, len(completed), comparison.num_windows)
        with CompletionDistributionClient(reference, timeout=source.timeout_seconds) as client:
            for index, tokens in enumerate(saved["windows"]):
                if index in completed:
                    continue
                values = np.stack([client.logprobs(tokens[:position], vocabulary_size) for position in score_positions])
                checkpoint.save_reference(index, values)
    return saved


def run_distribution_comparison(
    config: EvaluationConfig, comparison: DistributionComparisonConfig,
) -> None:
    """Compare models sequentially, retaining complete windows for resume and rebuilding reports.

    Each invocation owns a new result directory. Resume snapshots the previous checkpoint
    without modifying it; only unfinished windows require model execution. Temporary
    deployments retain the shared service lifecycle and are released between models.
    """

    reference_source = comparison.reference_source(config.service)
    record = {"mode": "distribution_comparison"}
    points: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    environments: dict[str, Any] = {"candidates": []}
    reference_model = reference_source.model
    failure: BaseException | None = None
    started = time.monotonic()
    with ResultOutputs(
        config, None, directory_prefix="distribution-comparison-",
        sink_factory=lambda directory: distribution_comparison_sinks(config, record, directory),
    ) as outputs:
        outputs.open(record)
        directory = Path(outputs.execution_dir)
        protocol = comparison.protocol()
        try:
            score_positions = range(comparison.context_length - comparison.score_tokens, comparison.context_length)
            with DistributionComparisonCheckpoint(
                directory, config.resume, comparison.checkpoint_settings(config.service),
            ) as checkpoint:
                reference = _score_reference(reference_source, comparison, checkpoint)
                reference_model = reference["model"]
                protocol = reference["protocol"]
                windows = reference["windows"]
                vocabulary_size = protocol["model_vocab_size"]
                environments["reference"] = reference["environment"]
                labels: set[str] = set()
                for candidate_index, candidate in enumerate(comparison.candidates):
                    saved = checkpoint.get(f"candidate/{candidate_index}")
                    completed = checkpoint.candidate_windows(candidate_index)
                    if len(completed) < len(windows):
                        with resolve_model_service(candidate.service) as service:
                            metadata = candidate.metadata(service.model)
                            if not candidate.method and service.deployment is not None:
                                methods = set()
                                for document in service.deployment.objects:
                                    if document["kind"] == "ModelService" and document["spec"]["model"] == service.model:
                                        args = document["spec"].get("engineArgs", {})
                                        methods.add(args.get("quantization") or args.get("dtype", "unspecified"))
                                if len(methods) == 1:
                                    metadata["method"] = methods.pop()
                            if saved is not None and saved["metadata"] != metadata:
                                raise ValueError("Candidate model or method differs from the saved comparison")
                            saved = {"metadata": metadata, "environment": serving_environment(service)}
                            checkpoint.put(f"candidate/{candidate_index}", saved)
                            logger.info("Candidate: %s | %d/%d windows already complete", metadata["label"], len(completed), len(windows))
                            with CompletionDistributionClient(service, timeout=candidate.service.timeout_seconds) as client:
                                for window_index, tokens in enumerate(windows):
                                    if window_index in completed:
                                        continue
                                    reference_values = checkpoint.reference(window_index, (comparison.score_tokens, vocabulary_size))
                                    rows = []
                                    for score_index, position in enumerate(score_positions):
                                        values = client.logprobs(tokens[:position], vocabulary_size)
                                        rows.append({
                                            "candidate": metadata["label"], "window": window_index,
                                            "position": position, "scored_index": window_index * comparison.score_tokens + score_index,
                                            **compare_logprobs(reference_values[score_index], values, tokens[position], comparison.top_k),
                                        })
                                    checkpoint.save_candidate(candidate_index, window_index, rows)
                                    completed[window_index] = rows
                    metadata = saved["metadata"]
                    if metadata["label"] in labels:
                        raise ValueError("Candidate labels must be distinct; set label in each --candidates row")
                    labels.add(metadata["label"])
                    environments["candidates"].append({"label": metadata["label"], **saved["environment"]})
                    candidate_rows = [row for index in sorted(completed) for row in completed[index]]
                    positions.extend(candidate_rows)
                    points.append({**metadata, "vocab_size": vocabulary_size, **_candidate_summary(candidate_rows, comparison.top_k)})
        except (DeploymentError, ValueError, OSError, httpx.HTTPError, KeyboardInterrupt) as error:
            failure = error
            logger.error("Distribution comparison stopped: %s", error)
        metrics = {
            "duration_seconds": time.monotonic() - started,
            "scores": _score_rows(points, comparison.top_k),
            "execution": {"distribution_comparison": {
                "requested": len(comparison.candidates), "succeeded": len(points),
                "errored": int(failure is not None), "incomplete": failure is not None,
            }},
            "distribution_comparison": {
                "reference_model": reference_model, "protocol": protocol,
                "candidates": points, "positions": positions,
            },
        }
        artifacts = {
            "serving_environments": write_json(str(directory), "serving_environments.json", environments),
        }
        exit_code = 130 if isinstance(failure, KeyboardInterrupt) else int(failure is not None)
        outputs.publish(BenchmarkRun(
            record=record, metrics=metrics, measurements=None, artifacts=artifacts,
            exit_code=exit_code,
        ))
        if failure is not None:
            raise SystemExit(exit_code) from failure
