# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Resolve Hugging Face dataset selectors, dataset file URIs, and tokenizer repositories."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

# Remote tokenizers download only files needed for tokenization and decoding.
_TOKENIZER_ALLOW_PATTERNS = (
    "tokenizer*",
    "vocab*",
    "merges*",
    "special_tokens_map*",
    "added_tokens*",
    "chat_template*",
    "tokenization*",
    "config.json",
)

_HF_DATASETS_PREFIX = "hf://datasets/"
_HF_FILE_URI_FORMAT = "hf://datasets/<org>/<repo>[@<revision>]/<path>"


def _configured_hub_cache_dir() -> str | None:
    """Return the Hugging Face cache directory selected by the runtime environment."""
    for variable in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        value = os.environ.get(variable)
        if value:
            return str(Path(value).expanduser())

    home = os.environ.get("HF_HOME")
    if home:
        return str(Path(home).expanduser() / "hub")

    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        return str(Path(xdg_cache).expanduser() / "huggingface" / "hub")
    return None


def resolve_tokenizer_path(tokenizer_path: str) -> str:
    """Return a local tokenizer path, downloading a Hub repository when needed."""
    local = Path(tokenizer_path).expanduser()
    if local.exists():
        return str(local.resolve())
    if local.is_absolute() or tokenizer_path.startswith(("./", "../", "~")):
        raise ValueError(
            f"Tokenizer path does not exist locally: {tokenizer_path!r}; "
            "pass an existing directory or a Hugging Face repository ID"
        )

    from huggingface_hub import snapshot_download

    cache_dir = _configured_hub_cache_dir()
    logger.info(
        "Resolving tokenizer from Hugging Face repo %r%s",
        tokenizer_path,
        f" into {cache_dir!r}" if cache_dir else "",
    )
    download_args: dict[str, Any] = {
        "repo_id": tokenizer_path,
        "allow_patterns": list(_TOKENIZER_ALLOW_PATTERNS),
    }
    if cache_dir:
        # Hugging Face resolves its default cache during import, so pass the
        # runtime-selected directory explicitly for remote benchmark processes.
        download_args["cache_dir"] = cache_dir
    return snapshot_download(**download_args)


def is_hf_file_uri(source: str) -> bool:
    """Return whether a source is a canonical Hugging Face dataset file URI."""
    return source.startswith(_HF_DATASETS_PREFIX)


def parse_hf_file_uri(uri: str) -> tuple[str, Optional[str], str]:
    """Parse a Hugging Face dataset file URI into repository, revision, and path."""
    if not is_hf_file_uri(uri):
        raise ValueError(
            f"Invalid HF file URI {uri!r}. Use {_HF_FILE_URI_FORMAT}."
        )
    remainder = uri[len(_HF_DATASETS_PREFIX) :]
    if not remainder or remainder.startswith("/"):
        raise ValueError(
            f"Invalid HF file URI {uri!r}. Use {_HF_FILE_URI_FORMAT}."
        )
    if "@" in remainder:
        repo_id, after_at = remainder.split("@", 1)
        if not repo_id or "/" not in after_at:
            raise ValueError(
                f"Invalid HF file URI {uri!r}. Use {_HF_FILE_URI_FORMAT}."
            )
        revision, filename = after_at.split("/", 1)
        if not revision or not filename:
            raise ValueError(
                f"Invalid HF file URI {uri!r}. Use {_HF_FILE_URI_FORMAT}."
            )
        return repo_id, revision, filename
    parts = remainder.split("/")
    if len(parts) >= 3 and all(parts):
        return f"{parts[0]}/{parts[1]}", None, "/".join(parts[2:])
    if len(parts) == 2 and all(parts):
        return parts[0], None, parts[1]
    raise ValueError(
        f"Invalid HF file URI {uri!r}. Use {_HF_FILE_URI_FORMAT}."
    )


def resolve_hf_file_uri(uri: str) -> str:
    """Download a Hugging Face dataset file and return its local cache path."""
    from huggingface_hub import hf_hub_download

    repo_id, revision, filename = parse_hf_file_uri(uri)
    return hf_hub_download(
        repo_id=repo_id,
        filename=filename,
        repo_type="dataset",
        revision=revision,
    )


def parse_hf_dataset_spec(spec: str) -> tuple[str, str]:
    """Parse a Hugging Face dataset selector into dataset ID and split/config."""
    if ":" not in spec:
        raise ValueError(
            f"Invalid Hugging Face dataset spec {spec!r}. "
            "Use 'org/name:split' (split is required)."
        )
    dataset_id, split = spec.rsplit(":", 1)
    if not dataset_id or not split:
        raise ValueError(
            f"Invalid Hugging Face dataset spec {spec!r}. Use 'org/name:split'."
        )
    return dataset_id, split


def is_hf_dataset_spec(spec: str) -> bool:
    """Return whether a selector names a supported Hugging Face dataset."""
    try:
        parse_hf_dataset_spec(spec)
    except ValueError:
        return False
    return True


def _load_hf_data(dataset_id: str, split: str) -> Any:
    """Stream one Hugging Face split or builder configuration."""
    from datasets import get_dataset_config_names, get_dataset_split_names, load_dataset

    configs = get_dataset_config_names(dataset_id)
    if split in configs:
        data_splits = get_dataset_split_names(dataset_id, split)
        if len(data_splits) != 1:
            raise ValueError(
                f"Hugging Face dataset {dataset_id!r} config {split!r} has "
                f"multiple data splits {data_splits}; expected exactly one."
            )
        return load_dataset(
            dataset_id, name=split, split=data_splits[0], streaming=True
        )
    return load_dataset(dataset_id, split=split, streaming=True)


def iter_hf_rows(spec: str) -> Iterator[tuple[int, Any]]:
    """Yield zero-based row indexes and values from a Hugging Face selector."""
    dataset_id, split = parse_hf_dataset_spec(spec)
    for row_index, row in enumerate(_load_hf_data(dataset_id, split)):
        yield row_index, dict(row)
