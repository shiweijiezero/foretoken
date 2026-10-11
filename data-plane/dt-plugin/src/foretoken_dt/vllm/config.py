# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Configure native vLLM with the DT-owned scheduler and GPU worker."""

from importlib.metadata import version
from typing import Literal

from vllm import AsyncEngineArgs
from vllm.config import SpeculativeConfig
from vllm.config.utils import config

from . import (
    METAX_PLUGIN_VERSION,
    METAX_VLLM_VERSION,
    SUPPORTED_VLLM_VERSION,
    supported_runtime,
)


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
        """Use native neural profiles or token-only external rejection configuration."""
        if self.speculative_config is None:
            return None
        if self.speculative_config.get("method") != "external":
            return super().create_speculative_config(
                target_model_config, target_parallel_config
            )
        return ExternalSpeculativeConfig(
            **self.speculative_config,
            target_model_config=target_model_config,
            target_parallel_config=target_parallel_config,
        )

    def create_engine_config(self, *args, **kwargs):
        """Validate the supported engine boundary before spawning GPU processes."""
        installed = version("vllm")
        if not supported_runtime():
            raise RuntimeError(
                f"DT requires vLLM {SUPPORTED_VLLM_VERSION}, or MetaX "
                f"vLLM {METAX_VLLM_VERSION} with vllm-metax {METAX_PLUGIN_VERSION}; "
                f"found vLLM {installed}"
            )
        self.scheduler_cls = "foretoken_dt.vllm.scheduler.ObservedScheduler"
        context_role = (self.additional_config or {}).get("foretoken_dt_context_role")
        if context_role and self.speculative_config.get("enable_adaptive_verification"):
            # Native confidence scheduling belongs to the verifier. Remote
            # proposals currently carry token IDs and q, not confidences.
            raise ValueError("neural DT requires enable_adaptive_verification=false")
        if self.speculative_config is not None and context_role != "draft":
            self.scheduler_cls = "foretoken_dt.vllm.scheduler.ExternalScheduler"
        result = super().create_engine_config(*args, **kwargs)
        if context_role:
            spec = result.speculative_config
            if installed != SUPPORTED_VLLM_VERSION or spec.method not in (
                "dflash",
                "dspark",
                "eagle",
                "eagle3",
                "mtp",
            ):
                raise ValueError(
                    "Target context requires NVIDIA DFlash, DSpark, EAGLE or MTP"
                )
            if spec.method == "dflash" and spec.draft_model_config.architectures != [
                "DFlashDraftModel"
            ]:
                raise ValueError("this context adapter requires DFlashDraftModel")
            if spec.method == "dspark" and spec.draft_model_config.architectures != [
                "Qwen3DSparkModel"
            ]:
                raise ValueError("this context adapter requires Qwen3DSparkModel")
            if spec.use_gemma4_mtp() or spec.use_multi_module_mtp():
                raise ValueError("Target context does not yet support this MTP variant")
            from vllm.model_executor.model_loader.utils import get_model_cls

            if hasattr(
                get_model_cls(result.model_config), "get_mtp_target_hidden_states"
            ):
                raise ValueError("Target context does not yet support HC residuals")
            if (
                result.model_config.quantization
                or result.model_config.is_multimodal_model
            ):
                raise ValueError(
                    "neural DT currently requires an unquantized text Target"
                )
            if result.kv_transfer_config is not None:
                raise ValueError(
                    "neural DT context is not yet included in Target migration"
                )
            # A cache hit would skip the hidden states retained by TargetContext.
            # Autoregressive draft KV includes the next token, including an anchor
            # outside the hashed prompt. Token-only APC keys cannot identify it.
            if context_role == "target" or spec.method not in ("dflash", "dspark"):
                result.cache_config.enable_prefix_caching = False
        if installed == SUPPORTED_VLLM_VERSION:
            # Eager execution disables native JIT warmup by default. DT admits
            # random-sampling requests, so compile those kernels before readiness
            # rather than blocking an active round on its first sampling call.
            result.kernel_config.enable_jit_warmup = True
        # Platform configuration must run before DT wraps its selected Worker.
        workers = {
            "vllm.v1.worker.gpu_worker.Worker": "foretoken_dt.vllm.worker.Worker",
            "vllm_metax.v1.worker.gpu_worker.MacaWorker": "foretoken_dt.vllm.metax.Worker",
        }
        selected = result.parallel_config.worker_cls
        if selected not in workers:
            raise ValueError(f"DT has no adapter for platform worker {selected}")
        result.parallel_config.worker_cls = workers[selected]
        if not result.use_v2_model_runner:
            raise ValueError("DT requires Model Runner V2")
        if (
            result.parallel_config.world_size != 1
            or result.parallel_config.data_parallel_size != 1
            or result.scheduler_config.async_scheduling
            or not result.model_config.enforce_eager
            or result.scheduler_config.stream_interval != 1
            or (
                result.kv_transfer_config is not None
                and (
                    installed != SUPPORTED_VLLM_VERSION
                    or result.kv_transfer_config.kv_connector
                    != "DraftTargetKvConnector"
                    or result.kv_transfer_config.kv_connector_module_path
                    != "foretoken_dt.vllm.migration"
                )
            )
            or result.ec_transfer_config is not None
        ):
            raise ValueError(
                "DT requires one eager worker, synchronous local scheduling, "
                "stream_interval=1, no EC transfer and only the DT migration KV connector"
            )
        return result
