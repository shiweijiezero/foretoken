#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Prepare model and tokenizer snapshots in the configured Hugging Face cache."""

from __future__ import annotations

import argparse
from pathlib import Path

from huggingface_hub import snapshot_download


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--model-revision", required=True)
    parser.add_argument("--tokenizer", required=True)
    parser.add_argument("--tokenizer-revision", required=True)
    parser.add_argument("--offline", action="store_true")
    return parser


def _prepare(repo_id: str, revision: str, offline: bool) -> None:
    if Path(repo_id).is_dir():
        return
    snapshot_download(
        repo_id=repo_id,
        revision=revision,
        local_files_only=offline,
    )


def main() -> None:
    """Prepare each distinct Hugging Face snapshot required by one ModelService."""
    args = _parser().parse_args()
    snapshots = {
        (args.model, args.model_revision),
        (args.tokenizer, args.tokenizer_revision),
    }
    for repo_id, revision in sorted(snapshots):
        _prepare(repo_id, revision, args.offline)


if __name__ == "__main__":
    main()
