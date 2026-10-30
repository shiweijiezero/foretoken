# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Dispatch provider resolution and acquisition for the model preparation worker."""

import contextlib
import json
import logging
import os
import sys

from foretoken_model_sources import (
    AcquisitionError,
    acquire_in_parallel,
    huggingface,
    modelscope,
)


def main() -> None:
    """Emit one structured provider result while SDK output remains on stderr."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    source = os.environ["FORETOKEN_PREPARE_SOURCE"]
    if source == "hf":
        provider = huggingface
    elif source == "modelscope":
        provider = modelscope
    else:
        raise ValueError(f"unsupported preparation source: {source}")
    inputs = {
        "model": (
            os.environ["FORETOKEN_PREPARE_MODEL"],
            os.environ["FORETOKEN_PREPARE_MODEL_REVISION"],
        ),
        "tokenizer": (
            os.environ["FORETOKEN_PREPARE_TOKENIZER"],
            os.environ["FORETOKEN_PREPARE_TOKENIZER_REVISION"],
        ),
    }
    if draft := os.environ.get("FORETOKEN_PREPARE_DRAFT_MODEL"):
        inputs["draft"] = (draft, os.environ["FORETOKEN_PREPARE_DRAFT_REVISION"])
    artifacts = list(dict.fromkeys(inputs.values()))
    resolving = os.environ["FORETOKEN_PREPARE_ACTION"] == "resolve"
    operation = provider.resolve if resolving else provider.snapshot
    # Model, tokenizer, and a shared-checkpoint Draft acquire each source only once.
    with contextlib.redirect_stdout(sys.stderr):
        outputs = dict(
            zip(
                artifacts,
                acquire_in_parallel(
                    lambda artifact: operation(*artifact),
                    artifacts,
                    max_workers=len(artifacts),
                ),
            )
        )
    result = {}
    for name, artifact in inputs.items():
        if resolving:
            result[f"{name}_revision"] = outputs[artifact]
        else:
            result[name], result[f"{name}_files"] = outputs[artifact]
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except AcquisitionError:
        raise SystemExit(1) from None
