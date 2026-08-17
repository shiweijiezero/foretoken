# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Load HuggingFace dataset rows for ``--dataset org/name:split``."""

from __future__ import annotations

from typing import Any, Iterator

from datasets import get_dataset_config_names, get_dataset_split_names, load_dataset


def parse_hf_dataset_spec(spec: str) -> tuple[str, str]:
    """Parse ``org/name:split`` into ``(dataset_id, split)``.

    ``split`` is required and may be a non-standard name (not only
    ``train`` / ``test`` / ``validation``).
    """
    if ":" not in spec:
        raise ValueError(
            f"Invalid HuggingFace dataset spec {spec!r}. "
            "Use 'org/name:split' (split is required)."
        )
    dataset_id, split = spec.rsplit(":", 1)
    if not dataset_id or not split:
        raise ValueError(
            f"Invalid HuggingFace dataset spec {spec!r}. "
            "Use 'org/name:split'."
        )
    return dataset_id, split


def _load_hf_data(dataset_id: str, split: str) -> Any:
    """Load a streaming HF dataset for ``dataset_id`` / ``split``.

    Some hubs publish named builder configs whose only data split is
    ``train``; the CLI ``split`` then selects that config name.
    """
    configs = get_dataset_config_names(dataset_id)
    if split in configs:
        data_splits = get_dataset_split_names(dataset_id, split)
        if len(data_splits) != 1:
            raise ValueError(
                f"HuggingFace dataset {dataset_id!r} config {split!r} has "
                f"multiple data splits {data_splits}; expected exactly one."
            )
        return load_dataset(
            dataset_id,
            name=split,
            split=data_splits[0],
            streaming=True,
        )
    return load_dataset(dataset_id, split=split, streaming=True)


def iter_hf_rows(spec: str) -> Iterator[tuple[int, Any]]:
    """Yield ``(row_index, row_dict)`` from a HuggingFace dataset spec."""
    dataset_id, split = parse_hf_dataset_spec(spec)
    data = _load_hf_data(dataset_id, split)
    for row_index, row in enumerate(data):
        yield row_index, dict(row)
