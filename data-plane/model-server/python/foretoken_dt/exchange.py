# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Keep DT tensor allocations under vLLM's Mooncake registration owner."""

import os

import torch
from vllm.distributed.ec_transfer.ec_connector.mooncake.transfer import (
    MooncakeTransfer,
    ensure_mooncake_available,
)
from vllm.utils.network_utils import get_ip

from .protocol import TensorRegion

_DTYPES = {
    "float16": torch.float16,
    "bfloat16": torch.bfloat16,
    "float32": torch.float32,
}


class TensorExchange:
    """Own a role's registered host buffers while Mooncake owns the data transfers.

    Registered staging keeps the same TCP/RDMA path usable without requiring a
    device-specific transfer implementation. Native GPU streams own the copies
    between model tensors and these buffers.
    """

    def __init__(self, protocol: str):
        if protocol == "tcp" and os.environ.get("MC_TCP_PROTO") == "1":
            raise ValueError(
                "DT requires acknowledged TCP writes; unset MC_TCP_PROTO=1"
            )
        ensure_mooncake_available()
        self.transport = MooncakeTransfer(get_ip(), protocol)
        self.transport.ensure_ready()
        self.buffers: dict[str, torch.Tensor] = {}

    def allocate(self, name: str, shape: list[int], dtype: str) -> TensorRegion:
        """Register one fixed allocation until its peer has completed all uses."""
        if name in self.buffers:
            raise ValueError("DT allocation name is already registered")
        if any(size <= 0 for size in shape):
            raise ValueError("DT tensor dimensions must be positive")
        tensor = torch.empty(shape, dtype=_DTYPES[dtype], device="cpu", pin_memory=True)
        self.transport.acquire_sources([tensor])
        self.buffers[name] = tensor
        return TensorRegion(
            session=self.transport.local_session(),
            address=tensor.data_ptr(),
            shape=shape,
            dtype=dtype,
        )

    def write(self, name: str, rows: int, destination: TensorRegion) -> None:
        """Copy a registered row prefix and return only after native transfer completion."""
        tensor = self.buffers[name]
        if (
            list(tensor.shape) != destination.shape
            or tensor.dtype != _DTYPES[destination.dtype]
        ):
            raise ValueError("DT peer buffer layout differs")
        if not 0 <= rows <= tensor.shape[0]:
            raise ValueError("DT write exceeds its registered buffer")
        if rows:
            nbytes = rows * tensor[0].numel() * tensor.element_size()
            self.transport.write(
                destination.session,
                [tensor.data_ptr()],
                [destination.address],
                [nbytes],
            )

    def write_plane(
        self, name: str, plane: int, rows: int, destination: TensorRegion
    ) -> None:
        """Publish one contiguous feature plane after its producer event completes."""
        tensor = self.buffers[name]
        if (
            list(tensor.shape) != destination.shape
            or tensor.dtype != _DTYPES[destination.dtype]
        ):
            raise ValueError("DT peer buffer layout differs")
        if not 0 <= rows <= tensor.shape[1]:
            raise ValueError("DT feature write exceeds its registered buffer")
        if rows:
            source = tensor[plane, :rows]
            offset = source.data_ptr() - tensor.data_ptr()
            self.transport.write(
                destination.session,
                [source.data_ptr()],
                [destination.address + offset],
                [source.nbytes],
            )

    def release(self, name: str) -> None:
        """Release one allocation after its peer and local consumers have stopped using it."""
        tensor = self.buffers.get(name)
        if tensor is None:
            return
        if not self.transport.release_sources([tensor.data_ptr()]):
            raise RuntimeError("Mooncake retained a DT buffer registration")
        del self.buffers[name]

    def close(self) -> None:
        """Keep every buffer alive until native transport shutdown has joined all remaining IO."""
        self.transport.close()
        self.buffers.clear()
