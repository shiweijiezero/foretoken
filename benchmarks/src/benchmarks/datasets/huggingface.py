# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Resolve Hugging Face dataset selectors, dataset file URIs, and tokenizer repositories."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from typing import Any

from datasets import (
    get_dataset_config_names,
    get_dataset_split_names,
    load_dataset,
    load_dataset_builder,
)
from foretoken.network_sources import select_huggingface_endpoint
from huggingface_hub import hf_hub_download, snapshot_download
from huggingface_hub.errors import LocalEntryNotFoundError

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


def _configured_local_model(tokenizer_path: str) -> str | None:
    """Find a tokenizer directory below the benchmark data root when present."""
    data_root = os.environ.get("FORETOKEN_DATA_ROOT")
    if not data_root or "/" not in tokenizer_path:
        return None
    root = Path(data_root).expanduser()
    for candidate in (root / "models" / tokenizer_path, root / tokenizer_path):
        if candidate.is_dir() and any(candidate.glob("tokenizer*")):
            return str(candidate.resolve())
    return None


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


@contextmanager
def _hub_xet_disabled(disabled: bool) -> Iterator[None]:
    """Temporarily disable Xet for mirrors that do not provide CAS credentials."""
    if not disabled:
        yield
        return
    from huggingface_hub import constants

    previous_environment = os.environ.get("HF_HUB_DISABLE_XET")
    previous_constant = constants.HF_HUB_DISABLE_XET
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    constants.HF_HUB_DISABLE_XET = True
    try:
        yield
    finally:
        constants.HF_HUB_DISABLE_XET = previous_constant
        if previous_environment is None:
            os.environ.pop("HF_HUB_DISABLE_XET", None)
        else:
            os.environ["HF_HUB_DISABLE_XET"] = previous_environment


def resolve_tokenizer_path(tokenizer_path: str, *, source: str = "hf") -> str:
    """Resolve client-side text artifacts from a local directory or the selected model hub."""
    local = Path(tokenizer_path).expanduser()
    if local.exists():
        return str(local.resolve())
    if source == "local" or local.is_absolute() or tokenizer_path.startswith(("./", "../", "~")):
        raise ValueError(
            f"Tokenizer path does not exist locally: {tokenizer_path!r}; "
            "pass an existing directory or a Hugging Face repository ID"
        )

    if source == "modelscope":
        from modelscope.hub.snapshot_download import (
            snapshot_download as modelscope_download,
        )

        return modelscope_download(tokenizer_path, allow_file_pattern=list(_TOKENIZER_ALLOW_PATTERNS))
    if source != "hf":
        raise ValueError(f"Unsupported model source: {source!r}")
    local_model = _configured_local_model(tokenizer_path)
    if local_model:
        logger.info("Using local tokenizer for Hugging Face repo %r", tokenizer_path)
        return local_model
    cache_dir = _configured_hub_cache_dir()
    download_args: dict[str, Any] = {
        "repo_id": tokenizer_path,
        "allow_patterns": list(_TOKENIZER_ALLOW_PATTERNS),
        "local_files_only": True,
    }
    if cache_dir:
        # Hugging Face resolves its default cache during import, so pass the
        # runtime-selected directory explicitly for remote benchmark processes.
        download_args["cache_dir"] = cache_dir
    try:
        local = snapshot_download(**download_args)
        logger.info("Using cached tokenizer for Hugging Face repo %r", tokenizer_path)
        return local
    except LocalEntryNotFoundError:
        pass

    endpoint = select_huggingface_endpoint(
        tokenizer_path,
        os.environ.get("FORETOKEN_HF_ENDPOINT") or os.environ.get("HF_ENDPOINT"),
    )
    if endpoint:
        download_args["endpoint"] = endpoint
    download_args["local_files_only"] = False
    logger.info(
        "Resolving tokenizer from Hugging Face repo %r%s%s",
        tokenizer_path,
        f" into {cache_dir!r}" if cache_dir else "",
        f" via {endpoint}" if endpoint else "",
    )
    use_mirror = endpoint == "https://hf-mirror.com"
    with _hub_xet_disabled(use_mirror):
        return snapshot_download(**download_args)


def is_hf_file_uri(source: str) -> bool:
    """Return whether a source is a canonical Hugging Face dataset file URI."""
    return source.startswith(_HF_DATASETS_PREFIX)


def parse_hf_file_uri(uri: str) -> tuple[str, str | None, str]:
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
    repo_id, revision, filename = parse_hf_file_uri(uri)
    return hf_hub_download(
        repo_id=repo_id,
        filename=filename,
        repo_type="dataset",
        revision=revision,
    )


def parse_hf_dataset_spec(spec: str) -> tuple[str, str | None]:
    """Parse a repository ID and optional explicit split or configuration."""
    dataset_id, separator, selection = spec.partition(":")
    parts = dataset_id.split("/")
    if len(parts) != 2 or any(
        not part or not all(char.isalnum() or char in "-_." for char in part) or part in {".", ".."}
        for part in parts
    ):
        raise ValueError(f"Invalid Hugging Face dataset: {spec!r}; use org/name[:split]")
    if separator and not selection:
        raise ValueError(f"Empty dataset selection in {spec!r}")
    return dataset_id, selection if separator else None


def is_hf_dataset_spec(spec: str) -> bool:
    """Distinguish repository selectors from local paths and file URIs."""
    if spec.startswith(("/", "./", "../", "~")) or spec.endswith((".jsonl", ".json")):
        return False
    try:
        parse_hf_dataset_spec(spec)
    except ValueError:
        return False
    return True


@cache
def resolve_hf_dataset_spec(spec: str) -> tuple[str, str, str]:
    """Resolve the upstream default configuration and sole split, or require a choice.

    Dataset metadata, not a repository-name lookup table, owns the defaults.
    The resolved identity is also used when binding trace rows to dataset rows.
    """
    dataset_id, selection = parse_hf_dataset_spec(spec)
    configs = get_dataset_config_names(dataset_id)
    if selection in configs:
        config = selection
        split = None
    else:
        # The builder selects the repository's declared default configuration.
        # Its own error lists configurations when no default is available.
        config = load_dataset_builder(dataset_id).config.name
        split = selection
    splits = get_dataset_split_names(dataset_id, config)
    if split is None:
        if len(splits) != 1:
            choices = ", ".join(splits)
            raise ValueError(f"Dataset {dataset_id!r} has multiple splits ({choices}); specify org/name:split")
        split = splits[0]
    if split not in splits:
        raise ValueError(f"Unknown split {split!r} for {dataset_id!r}; choose from {', '.join(splits)}")
    return dataset_id, config, split


def same_dataset_source(left: str, right: str) -> bool:
    """Compare selectors after resolving optional Hugging Face metadata choices."""
    if left == right:
        return True
    if Path(left).expanduser().is_file() or Path(right).expanduser().is_file():
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    if is_hf_dataset_spec(left) and is_hf_dataset_spec(right):
        return resolve_hf_dataset_spec(left) == resolve_hf_dataset_spec(right)
    return False


def iter_hf_rows(spec: str) -> Iterator[tuple[int, Any]]:
    """Stream rows from the selected Hugging Face configuration and split."""
    dataset_id, config, split = resolve_hf_dataset_spec(spec)
    data = load_dataset(dataset_id, name=config, split=split, streaming=True)
    for row_index, row in enumerate(data):
        yield row_index, dict(row)
