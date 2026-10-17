# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Preserve the MetaX Worker when its platform selects precision-debug hooks."""

from vllm_metax.v1.worker.gpu_worker import MacaWorker

from .worker import ExternalRunnerMixin


class Worker(ExternalRunnerMixin, MacaWorker):
    """Add external-candidate execution without replacing MetaX Worker behavior."""
