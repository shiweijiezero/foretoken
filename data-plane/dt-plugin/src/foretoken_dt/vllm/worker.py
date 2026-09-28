# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Select the DT Runner at the native Worker construction boundary."""

from vllm.v1.worker import gpu_worker
from vllm.v1.worker.gpu import model_runner

from foretoken_dt.vllm.runner import GPUModelRunner, replace_binding


class Worker(gpu_worker.Worker):
    """Retain native GPU ownership and initialize the plugin's MRV2 Runner."""

    def init_device(self):
        """Scope Runner factory replacement to this Worker's device setup."""
        with replace_binding(model_runner, "GPUModelRunner", GPUModelRunner):
            return super().init_device()

    def compile_or_warm_up_model(self):
        """Run native synthetic batches before external distributions exist."""
        self.model_runner.warming_up = True
        try:
            return super().compile_or_warm_up_model()
        finally:
            self.model_runner.warming_up = False
