# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Select the DT Runner at the native Worker construction boundary."""

import torch
from vllm.v1.worker import gpu_worker
from vllm.v1.worker.gpu import model_runner

from foretoken_dt.vllm.runner import GPUModelRunner, replace_binding


class ExternalRunnerMixin:
    """Install DT Runner hooks while preserving the selected platform Worker."""

    def init_device(self):
        """Scope Runner factory replacement to this Worker's device setup."""
        with replace_binding(model_runner, "GPUModelRunner", GPUModelRunner):
            return super().init_device()

    def dt_wait_execution(self) -> None:
        """Acknowledge work preceding the serialized abort utility before session release."""
        self.model_runner.main_stream.synchronize()

    def compile_or_warm_up_model(self):
        """Warm native execution with the sampling path used by real DT requests."""
        self.model_runner.warming_up = True
        sampler = self.model_runner.sampler
        capture_proposals = sampler.capture_proposals
        try:
            if (
                self.vllm_config.kernel_config.enable_jit_warmup
                and not self.model_runner.external_speculation
            ):
                # Draft uses seeded draws and exports q. Native synthetic requests
                # otherwise choose fused sampling, leaving this path cold at readiness.
                sampler.capture_proposals = True
                # Proposal capture normalizes a vocabulary row outside the native
                # sampler. Batch-invariant execution also JIT-compiles this operation.
                torch.log_softmax(
                    torch.zeros(
                        self.model_runner.vocab_size,
                        dtype=torch.float32,
                        device=self.device,
                    ),
                    dim=-1,
                )
            return super().compile_or_warm_up_model()
        finally:
            sampler.capture_proposals = capture_proposals
            self.model_runner.warming_up = False


class Worker(ExternalRunnerMixin, gpu_worker.Worker):
    """Retain the native GPU Worker with external-candidate Runner hooks."""
