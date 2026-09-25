# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Compare served models on shared teacher-forced prefixes and publish fidelity results."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterator

import httpx
import numpy as np

from foretoken.manifest import DeploymentError

from benchmarks.config.evaluation import EvaluationConfig
from benchmarks.config.fidelity import FidelityConfig
from benchmarks.datasets.conversations import iter_jsonl_rows
from benchmarks.datasets.huggingface import resolve_tokenizer_path
from benchmarks.integrations.distributions import CompletionDistributionClient, compare_logprobs
from benchmarks.model_service import resolve_model_service
from benchmarks.results.environment import serving_environment
from benchmarks.results.fidelity import fidelity_sinks
from benchmarks.results.output import BenchmarkRun, ResultOutputs, write_json

logger = logging.getLogger(__name__)


def _text_rows(config: FidelityConfig) -> Iterator[str]:
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
            raise ValueError(f"Fidelity dataset rows require text column {config.text_column!r}")
        value = row[config.text_column]
        if not isinstance(value, str):
            raise ValueError(f"Fidelity text column {config.text_column!r} must contain strings")
        if value.strip():
            yield value + "\n"


def _token_windows(config: FidelityConfig, tokenizer: Any) -> list[list[int]]:
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
    delta = np.asarray([row["reference_token_delta_p"] for row in rows], dtype=np.float64)
    return {
        "scored_positions": len(rows),
        "mean_kl": float(kl.mean()),
        "median_kl": float(np.median(kl)),
        "p99_kl": float(np.percentile(kl, 99, method="inverted_cdf")),
        "top1_agreement": float(np.mean([row["top1_match"] for row in rows])),
        **{f"top{k}_overlap": float(np.mean([row[f"top{k}_overlap"] for row in rows])) for k in top_k},
        "reference_token_mean_delta_p": float(delta.mean()),
        "reference_token_rms_delta_p": float(np.sqrt(np.mean(delta ** 2))),
        "mean_centered_logit_rmse": float(np.mean([row["centered_logit_rmse"] for row in rows])),
        "mean_total_variation": float(np.mean([row["total_variation"] for row in rows])),
    }


def _score_rows(points: list[dict[str, Any]], top_k: tuple[int, ...]) -> list[dict[str, Any]]:
    """Project comparison metrics onto the existing quality result table."""
    higher = {"top1_agreement", *(f"top{k}_overlap" for k in top_k)}
    names = [
        "mean_kl", "median_kl", "p99_kl", "top1_agreement",
        *(f"top{k}_overlap" for k in top_k),
        "reference_token_mean_delta_p", "reference_token_rms_delta_p",
        "mean_centered_logit_rmse", "mean_total_variation",
    ]
    return [{
        "task": point["label"], "level": "task", "subset": "", "filter": "teacher_forced",
        "metric": name, "value": point[name], "stderr": None,
        "samples": point["scored_positions"],
        "direction": "higher" if name in higher else (None if name == "reference_token_mean_delta_p" else "lower"),
        "display_multiplier": 100 if name in higher else 1,
        "display_unit": "%" if name in higher else "",
        "primary": name == "mean_kl",
    } for point in points for name in names]


def run_fidelity(config: EvaluationConfig, fidelity: FidelityConfig) -> None:
    """Score a reference once, compare each candidate, and retain scalar results and plots.

    Full distributions live only in a temporary local cache. Reference and candidate
    deployments are resolved sequentially through the existing service lifecycle;
    a temporary reference deployment is released before the candidate is prepared.
    Generation is a one-token probe: the next prefix always comes from corpus tokens.
    """
    from transformers import AutoConfig, AutoTokenizer

    reference_source = fidelity.reference_source(config.service)
    record = {"mode": "fidelity", "evaluator": "fidelity", "model": "model comparison"}
    points: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    environments: dict[str, Any] = {"candidates": []}
    reference_model = reference_source.model
    failure: BaseException | None = None
    started = time.monotonic()
    with ResultOutputs(
        config, None, directory_prefix="fidelity-",
        sink_factory=lambda directory: fidelity_sinks(config, record, directory),
    ) as outputs:
        outputs.open(record)
        directory = Path(outputs.execution_dir)
        protocol = fidelity.protocol()
        try:
            model_path = resolve_tokenizer_path(fidelity.tokenizer)
            tokenizer = AutoTokenizer.from_pretrained(model_path)
            # The output head can contain more entries than the tokenizer map.
            # Request its full size as a positive count for the public Completions API.
            model_config = AutoConfig.from_pretrained(model_path).to_dict()
            vocabulary_size = model_config.get("text_config", model_config)["vocab_size"]
            if max(tokenizer.get_vocab().values()) >= vocabulary_size:
                raise ValueError("Tokenizer token IDs exceed the model configuration's vocabulary size")
            windows = _token_windows(fidelity, tokenizer)
            protocol["bos_token_id"] = tokenizer.bos_token_id
            protocol["tokenizer_vocab_size"] = len(tokenizer)
            protocol["model_vocab_size"] = vocabulary_size
            score_positions = range(fidelity.context_length - fidelity.score_tokens, fidelity.context_length)
            with TemporaryDirectory(prefix=".reference-", dir=directory) as cache:
                cache_dir = Path(cache)
                with resolve_model_service(reference_source) as reference:
                    reference_model = reference.model
                    environments["reference"] = serving_environment(reference)
                    with CompletionDistributionClient(reference, timeout=reference_source.timeout_seconds) as client:
                        logger.info("Reference: %s | %d windows × %d scored positions", reference.model, len(windows), fidelity.score_tokens)
                        for window_index, tokens in enumerate(windows):
                            arrays = []
                            ids = None
                            for position in score_positions:
                                current_ids, values = client.logprobs(tokens[:position], vocabulary_size)
                                if ids is not None and not np.array_equal(ids, current_ids):
                                    raise ValueError("Reference vocabulary changed between scoring positions")
                                ids = current_ids
                                arrays.append(values)
                            np.savez(cache_dir / f"{window_index}.npz", ids=ids, logprobs=np.stack(arrays))
                labels: set[str] = set()
                for candidate in fidelity.candidates:
                    with resolve_model_service(candidate.service) as service:
                        metadata = candidate.metadata(service.model)
                        if metadata["label"] in labels:
                            raise ValueError("Candidate labels must be distinct; set label in each --candidates row")
                        labels.add(metadata["label"])
                        environments["candidates"].append({"label": metadata["label"], **serving_environment(service)})
                        candidate_rows = []
                        vocab_size = 0
                        logger.info("Candidate: %s | method=%s", metadata["label"], metadata["method"])
                        with CompletionDistributionClient(service, timeout=candidate.service.timeout_seconds) as client:
                            for window_index, tokens in enumerate(windows):
                                with np.load(cache_dir / f"{window_index}.npz", allow_pickle=False) as cached:
                                    ids, reference_values = cached["ids"], cached["logprobs"]
                                    vocab_size = len(ids)
                                    for score_index, position in enumerate(score_positions):
                                        candidate_ids, values = client.logprobs(tokens[:position], vocabulary_size)
                                        if not np.array_equal(ids, candidate_ids):
                                            raise ValueError("Reference and candidate must expose identical vocabulary token IDs")
                                        token_index = int(np.searchsorted(ids, tokens[position]))
                                        if token_index == len(ids) or ids[token_index] != tokens[position]:
                                            raise ValueError("Reference text token is absent from the returned vocabulary")
                                        row = {
                                            "candidate": metadata["label"], "window": window_index,
                                            "position": position, "scored_index": len(candidate_rows),
                                            **compare_logprobs(reference_values[score_index], values, token_index, fidelity.top_k),
                                        }
                                        candidate_rows.append(row)
                                        positions.append(row)
                        points.append({**metadata, "vocab_size": vocab_size, **_candidate_summary(candidate_rows, fidelity.top_k)})
        except (DeploymentError, ValueError, OSError, httpx.HTTPError, KeyboardInterrupt) as error:
            failure = error
            logger.error("Fidelity evaluation stopped: %s", error)
        metrics = {
            "duration_seconds": time.monotonic() - started,
            "scores": _score_rows(points, fidelity.top_k),
            "execution": {"fidelity": {
                "requested": len(fidelity.candidates), "succeeded": len(points),
                "errored": int(failure is not None), "incomplete": failure is not None,
            }},
            "fidelity": {
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
