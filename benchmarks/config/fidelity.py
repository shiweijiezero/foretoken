# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Configure paired output-distribution evaluation without changing task evaluators."""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Sequence

from benchmarks.config.benchmark import ModelServiceSource
from benchmarks.datasets.conversations import iter_jsonl_rows


@dataclass(frozen=True)
class FidelityCandidate:
    """One served model and optional coordinates used to label comparison plots."""

    service: ModelServiceSource
    label: str = ""
    method: str = ""
    weight_bits: float | None = None
    bits_per_weight: float | None = None
    model_size_gib: float | None = None

    def metadata(self, model: str) -> dict[str, Any]:
        """Describe a candidate for reports without exposing connection credentials."""
        return {
            "model": model,
            "label": self.label or (Path(self.service.kustomize_path).name if self.service.kustomize_path else model),
            "method": self.method or "unspecified",
            "weight_bits": self.weight_bits,
            "bits_per_weight": self.bits_per_weight,
            "model_size_gib": self.model_size_gib,
        }


@dataclass(frozen=True)
class FidelityConfig:
    """Own the reference, text windows, scoring positions, and candidate declarations."""

    reference_path: str
    reference_url: str
    reference_model: str
    reference_api_key: str | None
    tokenizer: str
    dataset: str
    dataset_config: str | None
    split: str
    text_column: str
    context_length: int
    num_windows: int
    score_tokens: int
    top_k: tuple[int, ...]
    candidates: tuple[FidelityCandidate, ...]

    def reference_source(self, default: ModelServiceSource) -> ModelServiceSource:
        """Resolve the reference connection, reusing the candidate connection when omitted."""
        source = replace(
            default,
            kustomize_path=self.reference_path or (default.kustomize_path if not self.reference_url else ""),
            url=self.reference_url or (default.url if not self.reference_path else ""),
            model=self.reference_model if self.reference_path or self.reference_url else self.reference_model or default.model,
            api_key=default.api_key if self.reference_api_key is None else self.reference_api_key,
        )
        source.validate()
        return source

    def protocol(self) -> dict[str, Any]:
        """Publish the text-selection and scoring protocol shared by every candidate."""
        return {
            "dataset": self.dataset,
            "dataset_config": self.dataset_config,
            "split": self.split,
            "text_column": self.text_column,
            "tokenizer": self.tokenizer,
            "context_length": self.context_length,
            "num_windows": self.num_windows,
            "score_tokens": self.score_tokens,
            "top_k": list(self.top_k),
            "kl_direction": "reference || candidate",
            "distribution": "full_vocabulary",
        }


def _positive_coordinate(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a positive number")
    return number


def add_fidelity_arguments(parser: argparse.ArgumentParser) -> None:
    """Define comparison choices for parsing and the combined eval compare help page."""
    parser.add_argument("--reference", default="", metavar="PATH", help="reference Kustomize deployment")
    parser.add_argument("--reference-url", default="", help="reference Chat Completions URL")
    parser.add_argument("--reference-model", default="", help="reference model ID; inferred for a single-model deployment")
    parser.add_argument("--reference-api-key", default=None, help="reference service API key (defaults to --api-key)")
    parser.add_argument("--tokenizer-path", default="", help="override reference text artifacts with a base-model repository or local directory containing tokenizer files and config.json")
    parser.add_argument("--dataset", default="Salesforce/wikitext", help="Hugging Face dataset or local text/JSONL file")
    parser.add_argument("--dataset-config", default=None, help="Hugging Face configuration; WikiText defaults to wikitext-2-raw-v1")
    parser.add_argument("--split", default="test", help="Hugging Face split (default: test)")
    parser.add_argument("--text-column", default="text", help="text field in each dataset row (default: text)")
    parser.add_argument("--context-length", type=int, default=512, help="tokens in each non-overlapping text window (default: 512)")
    parser.add_argument("--num-windows", type=int, default=4, help="number of complete text windows (default: 4)")
    parser.add_argument("--score-tokens", type=int, default=16, help="score this many final token positions per window (default: 16)")
    parser.add_argument("--top-k", type=int, nargs="+", default=[5, 10], help="token-set overlap sizes; Top-1 agreement is always reported")
    parser.add_argument("--candidates", default="", metavar="JSONL", help="candidate models and optional plot labels/coordinates")
    parser.add_argument("--label", default="", help="single-candidate display name")
    parser.add_argument("--method", default="", help="single-candidate quantization method label")
    parser.add_argument("--weight-bits", type=float, default=None, help="nominal weight precision for plotting; not effective bits/weight")
    parser.add_argument("--bits-per-weight", type=float, default=None, help="measured effective bits per weight, including quantization overhead")
    parser.add_argument("--model-size-gib", type=float, default=None, help="measured checkpoint size in GiB for plotting")


def parse_fidelity_arguments(arguments: Sequence[str], service: ModelServiceSource) -> FidelityConfig:
    """Resolve comparison choices and candidate declarations before serving starts."""
    parser = argparse.ArgumentParser(
        prog="foretoken eval compare", allow_abbrev=False,
        description="Compare full next-token distributions on identical text prefixes.",
    )
    add_fidelity_arguments(parser)
    options = parser.parse_args(arguments)
    if options.reference and options.reference_url:
        parser.error("use --reference or --reference-url, not both")
    if not (options.reference or options.reference_url or options.reference_model):
        parser.error("select a reference with --reference, --reference-url, or --reference-model")
    if options.num_windows < 1 or not 1 <= options.score_tokens < options.context_length:
        parser.error("require num-windows >= 1 and 1 <= score-tokens < context-length")
    if not options.top_k or any(k < 1 for k in options.top_k):
        parser.error("--top-k values must be positive")
    candidates = []
    if options.candidates:
        if options.label or options.method or any(value is not None for value in (options.weight_bits, options.bits_per_weight, options.model_size_gib)):
            parser.error("put labels and plot coordinates in --candidates rows when comparing multiple models")
        allowed = {"path", "url", "model", "label", "method", "weight_bits", "bits_per_weight", "model_size_gib"}
        for path, line, _, row in iter_jsonl_rows(options.candidates):
            if not isinstance(row, dict) or set(row) - allowed:
                raise ValueError(f"Candidate at {path}:{line} must contain only {', '.join(sorted(allowed))}")
            if row.get("path") and row.get("url"):
                raise ValueError(f"Candidate at {path}:{line} must select path or url")
            candidate_service = replace(
                service,
                kustomize_path=str(row.get("path") or (service.kustomize_path if not row.get("url") else "")),
                url=str(row.get("url") or (service.url if not row.get("path") else "")),
                model=str(row.get("model") or ("" if row.get("path") or row.get("url") else service.model)),
            )
            candidate_service.validate()
            candidates.append(FidelityCandidate(
                candidate_service,
                label=str(row.get("label") or ""),
                method=str(row.get("method") or ""),
                weight_bits=_positive_coordinate(row.get("weight_bits"), "weight_bits"),
                bits_per_weight=_positive_coordinate(row.get("bits_per_weight"), "bits_per_weight"),
                model_size_gib=_positive_coordinate(row.get("model_size_gib"), "model_size_gib"),
            ))
    else:
        service.validate()
        candidates.append(FidelityCandidate(
            service,
            label=options.label,
            method=options.method,
            weight_bits=_positive_coordinate(options.weight_bits, "weight_bits"),
            bits_per_weight=_positive_coordinate(options.bits_per_weight, "bits_per_weight"),
            model_size_gib=_positive_coordinate(options.model_size_gib, "model_size_gib"),
        ))
    if not candidates:
        parser.error("--candidates contains no models")
    dataset_config = options.dataset_config
    if dataset_config is None and options.dataset == "Salesforce/wikitext":
        dataset_config = "wikitext-2-raw-v1"
    config = FidelityConfig(
        reference_path=options.reference,
        reference_url=options.reference_url,
        reference_model=options.reference_model,
        reference_api_key=options.reference_api_key,
        tokenizer=options.tokenizer_path,
        dataset=options.dataset,
        dataset_config=dataset_config,
        split=options.split,
        text_column=options.text_column,
        context_length=options.context_length,
        num_windows=options.num_windows,
        score_tokens=options.score_tokens,
        top_k=tuple(sorted(set(options.top_k))),
        candidates=tuple(candidates),
    )
    config.reference_source(service)
    return config
