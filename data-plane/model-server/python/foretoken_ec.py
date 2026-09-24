# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Shared encoder-cache publication for concurrent inference workers."""

import os
import tempfile
from pathlib import Path

import safetensors.torch
from vllm.distributed.ec_transfer.ec_connector.example_connector import (
    ECExampleConnector,
)


class SharedStorageConnector(ECExampleConnector):
    """Reuse vLLM's encoder cache protocol with atomic shared-file publication."""

    def has_cache_item(self, identifier: str) -> bool:
        """Only consumers load shared tensors; producers rebuild evicted local entries."""
        return self.is_consumer and super().has_cache_item(identifier)

    def request_finished(self, request):
        """Return the published cache identities to the frontend's encoder barrier."""
        if not self.is_producer:
            return False, None
        return False, {
            "ec_items": [{"mm_hash": feature.identifier} for feature in request.mm_features]
        }

    def save_caches(self, encoder_cache, mm_hash, **kwargs) -> None:
        """Publish a complete tensor before another worker can discover its cache key."""
        if not self.is_producer:
            return
        destination = Path(self._generate_filename_debug(mm_hash))
        tensors = {"ec_cache": encoder_cache[mm_hash].detach().cpu()}
        # TP workers and simultaneous requests may publish the same cache entry.
        # A private file on the same filesystem keeps readers away from partial writes.
        with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
            source = Path(temporary) / destination.name
            safetensors.torch.save_file(tensors, str(source))
            os.replace(source, destination)
