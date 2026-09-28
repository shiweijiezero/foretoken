# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Configure native vLLM with the DT-owned scheduler and GPU worker."""

from importlib.metadata import version
from typing import Literal

from vllm import AsyncEngineArgs
from vllm.config import SpeculativeConfig
from vllm.config.utils import config

from . import SUPPORTED_VLLM_VERSION


@config
class ExternalSpeculativeConfig(SpeculativeConfig):
    """Describe external candidates without loading a local proposal model."""

    method: Literal["external"] = "external"

    def __post_init__(self):
        """Keep native field validation while excluding unsupported local drafting."""
        if self.model is not None:
            raise ValueError("DT Target does not load a local Draft model")
        if self.rejection_sample_method != "standard":
            raise ValueError("DT requires standard rejection sampling")
        if self.enable_adaptive_verification or self.parallel_drafting:
            raise ValueError("DT currently requires linear candidates")

    def __repr__(self) -> str:
        """Report the candidate budget without dereferencing absent Draft weights."""
        return (
            f"ExternalSpeculativeConfig(num_speculative_tokens={self.num_speculative_tokens}, "
            f"draft_sample_method={self.draft_sample_method!r})"
        )


class ExternalEngineArgs(AsyncEngineArgs):
    """Role-launcher arguments that select plugin classes through native factories."""

    def create_speculative_config(self, target_model_config, target_parallel_config):
        """Build the Target's external configuration; Draft uses ordinary decoding."""
        if self.speculative_config is None:
            return None
        return ExternalSpeculativeConfig(
            **self.speculative_config,
            target_model_config=target_model_config,
            target_parallel_config=target_parallel_config,
        )

    def create_engine_config(self, *args, **kwargs):
        """Validate the supported engine boundary before spawning GPU processes."""
        installed = version("vllm")
        if installed != SUPPORTED_VLLM_VERSION:
            raise RuntimeError(
                f"DT supports native vLLM {SUPPORTED_VLLM_VERSION}; found {installed}"
            )
        self.worker_cls = "foretoken_dt.vllm.worker.Worker"
        if self.speculative_config is not None:
            self.scheduler_cls = "foretoken_dt.vllm.scheduler.ExternalScheduler"
        result = super().create_engine_config(*args, **kwargs)
        if not result.use_v2_model_runner:
            raise ValueError("DT requires Model Runner V2")
        if (
            result.parallel_config.world_size != 1
            or result.parallel_config.data_parallel_size != 1
            or result.scheduler_config.async_scheduling
            or not result.model_config.enforce_eager
            or result.scheduler_config.stream_interval != 1
            or result.kv_transfer_config is not None
            or result.ec_transfer_config is not None
        ):
            raise ValueError(
                "DT requires one eager worker, synchronous local scheduling, "
                "stream_interval=1 and no KV/EC transfer"
            )
        return result
