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
        mctracer = self.worker_cls == "foretoken_mctracer.Worker"
        if mctracer:
            # Platform selection still owns its Worker; add capture to the DT
            # subclass after the native configuration has selected that base.
            self.worker_cls = "auto"
        installed = version("vllm")
        if not supported_runtime():
            raise RuntimeError(
                f"DT requires vLLM {SUPPORTED_VLLM_VERSION}, or MetaX "
                f"vLLM {METAX_VLLM_VERSION} with vllm-metax {METAX_PLUGIN_VERSION}; "
                f"found vLLM {installed}"
            )
        self.scheduler_cls = "foretoken_dt.vllm.scheduler.ObservedScheduler"
        draft = (self.additional_config or {}).get("foretoken_dt_role") == "draft"
        if draft:
            self.scheduler_cls = "foretoken_dt.vllm.scheduler.DraftScheduler"
        context_role = (self.additional_config or {}).get("foretoken_dt_context_role")
        if context_role and self.speculative_config.get("enable_adaptive_verification"):
            # Native confidence scheduling belongs to the verifier. Remote
            # proposals currently carry token IDs and q, not confidences.
            raise ValueError("neural DT requires enable_adaptive_verification=false")
        if self.speculative_config is not None and context_role != "draft":
            self.scheduler_cls = "foretoken_dt.vllm.scheduler.ExternalScheduler"
        # Native eager setup disables JIT warmup. DT warms sampling kernels before
        # readiness, while preserving the user's native kernel configuration.
        jit_warmup = (
            self.kernel_config.enable_jit_warmup
            if installed == SUPPORTED_VLLM_VERSION
            else None
        )
        result = super().create_engine_config(*args, **kwargs)
        if draft:
            # A retained request rewrites its unconfirmed tail in place. Keep
            # these pages private; token-only APC must not publish that mutable
            # tail or share neural KV conditioned on provisional features.
            result.cache_config.enable_prefix_caching = False
            if (
                result.model_config.has_inner_state
                or result.model_config.get_sliding_window()
            ):
                raise ValueError(
                    "retained Draft turns currently require full-attention KV"
                )
        if jit_warmup is not None:
            result.kernel_config.enable_jit_warmup = jit_warmup
        if context_role:
            spec = result.speculative_config
            if spec.method not in (
                "dflash",
                "dspark",
                "eagle",
                "eagle3",
                "mtp",
            ):
                raise ValueError(
                    "Target context requires native DFlash, DSpark, EAGLE or MTP"
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
            # A Target cache hit would skip hidden states needed by Draft replay.
            if context_role == "target":
                result.cache_config.enable_prefix_caching = False
        # Platform configuration must run before DT wraps its selected Worker.
        workers = {
            "vllm.v1.worker.gpu_worker.Worker": "foretoken_dt.vllm.worker.Worker",
            "vllm_metax.v1.worker.gpu_worker.MacaWorker": "foretoken_dt.vllm.metax.Worker",
        }
        selected = result.parallel_config.worker_cls
        if selected not in workers:
            raise ValueError(f"DT has no adapter for platform worker {selected}")
        result.parallel_config.worker_cls = workers[selected]
        if mctracer:
            if selected != "vllm_metax.v1.worker.gpu_worker.MacaWorker":
                raise ValueError("mcTracer requires the MetaX worker")
            result.parallel_config.worker_cls = "foretoken_dt.vllm.metax_profile.Worker"
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
