# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Compose the existing model-server collector with the platform-preserving DT worker."""

from foretoken_mctracer import WorkerMixin

from .metax import Worker as DtWorker


class Worker(WorkerMixin, DtWorker):
    """Retain native execution and DT preparation while exposing mcTracer collective RPC."""
