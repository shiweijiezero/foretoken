# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Pinned native-vLLM integration for Foretoken's independent DT roles."""

from importlib.metadata import PackageNotFoundError, version

SUPPORTED_VLLM_VERSION = "0.30.1rc1.dev194+g3b4566c5c"

METAX_VLLM_VERSION = "0.30.0.dev0"
METAX_PLUGIN_VERSION = "0.29.0.dev0"


def supported_runtime() -> bool:
    """Match the engine/platform pair before installing version-sensitive DT hooks."""
    installed = version("vllm")
    try:
        metax = version("vllm-metax")
    except PackageNotFoundError:
        return installed == SUPPORTED_VLLM_VERSION
    return (
        installed == METAX_VLLM_VERSION
        and metax.split("+", 1)[0] == METAX_PLUGIN_VERSION
    )


def register() -> None:
    """Install scoped EngineCore hooks in each vLLM process loading the plugin."""
    # Installation also exposes this entry point to ordinary vLLM services.
    # Only the DT launcher rejects unsupported versions; other services stay native.
    if not supported_runtime():
        return
    from .engine import install_engine_patches

    install_engine_patches()
