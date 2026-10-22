# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Provider-owned model acquisition and collection for model-server workers."""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)


class AcquisitionError(RuntimeError):
    """A provider pool failed after publishing its original diagnostic."""


def acquire_in_parallel(function, inputs, *, max_workers):
    """Return provider results in input order, reporting failure before joining remaining writes."""
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        pending = {}
        try:
            for index, value in enumerate(inputs):
                pending[executor.submit(function, value)] = index
            results = [None] * len(pending)
            for future in as_completed(pending):
                results[pending[future]] = future.result()
        except BaseException as error:
            for future in pending:
                future.cancel()
            if isinstance(error, AcquisitionError) or not isinstance(error, Exception):
                raise
            logger.error("Model source operation failed", exc_info=error)
            raise AcquisitionError(str(error)) from error
    return results
