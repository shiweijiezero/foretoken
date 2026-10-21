# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Dispatch provider resolution and acquisition for the model preparation worker."""

import contextlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from foretoken_model_sources import huggingface, modelscope


def main() -> None:
    """Emit one structured provider result while SDK output remains on stderr."""
    source = os.environ["FORETOKEN_PREPARE_SOURCE"]
    if source == "hf":
        provider = huggingface
    elif source == "modelscope":
        provider = modelscope
    else:
        raise ValueError(f"unsupported preparation source: {source}")
    model = (
        os.environ["FORETOKEN_PREPARE_MODEL"],
        os.environ["FORETOKEN_PREPARE_MODEL_REVISION"],
    )
    tokenizer = (
        os.environ["FORETOKEN_PREPARE_TOKENIZER"],
        os.environ["FORETOKEN_PREPARE_TOKENIZER_REVISION"],
    )
    artifacts = [model] if tokenizer == model else [model, tokenizer]
    with contextlib.redirect_stdout(sys.stderr):
        if os.environ["FORETOKEN_PREPARE_ACTION"] == "resolve":
            with ThreadPoolExecutor(max_workers=len(artifacts)) as executor:
                revisions = list(
                    executor.map(
                        lambda artifact: provider.resolve(*artifact), artifacts
                    )
                )
            result = {
                "model_revision": revisions[0],
                "tokenizer_revision": revisions[-1],
            }
        else:
            with ThreadPoolExecutor(max_workers=len(artifacts)) as executor:
                snapshots = list(
                    executor.map(
                        lambda artifact: provider.snapshot(*artifact), artifacts
                    )
                )
            result = {
                "model": snapshots[0][0],
                "tokenizer": snapshots[-1][0],
                "model_files": snapshots[0][1],
                "tokenizer_files": snapshots[-1][1],
            }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
