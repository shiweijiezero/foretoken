# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Pinned native-vLLM integration for Foretoken's independent DT roles."""

from importlib.metadata import version

SUPPORTED_VLLM_VERSION = "0.30.1rc1.dev194+g3b4566c5c"


def register() -> None:
    """Install scoped EngineCore hooks in each vLLM process loading the plugin."""
    # Installation also exposes this entry point to ordinary vLLM services.
    # Only the DT launcher rejects unsupported versions; other services stay native.
    if version("vllm") != SUPPORTED_VLLM_VERSION:
        return
    from .engine import install_engine_patches

    install_engine_patches()
