# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Own native GPU point-to-point transfers for one Draft/Target connection."""

import torch
from vllm import envs
from vllm.distributed.device_communicators.pynccl import PyNcclCommunicator
from vllm.distributed.device_communicators.pynccl_wrapper import NCCLLibrary


class NcclPeer:
    """Own independent Target-to-Draft and Draft-to-Target communicators.

    Rank 0 is the Target; rank 1 is the Draft. The control connection exchanges
    IDs and shapes. Model graphs produce tensors and events independently of
    the connection's lifetime.
    """

    def __init__(self, unique_ids: tuple[str, str], rank: int, device: torch.device):
        if envs.VLLM_DISABLE_PYNCCL:
            raise ValueError("DT NCCL requires the native PyNccl communicator")
        NCCLLibrary()
        self.unique_ids = unique_ids
        self.rank = rank
        self.device = device
        self.remote_rank = 1 - rank
        self.input_stream = torch.cuda.Stream(device)
        self.output_stream = torch.cuda.Stream(device)
        self.input_ready = torch.cuda.Event()
        self.output_ready = torch.cuda.Event()
        self.t2d_comm: PyNcclCommunicator | None = None
        self.d2t_comm: PyNcclCommunicator | None = None

    @staticmethod
    def create_unique_ids() -> tuple[str, str]:
        """Mint the two native rendezvous IDs sent by the Target control owner."""
        library = NCCLLibrary()
        return (
            bytes(library.ncclGetUniqueId()).hex(),
            bytes(library.ncclGetUniqueId()).hex(),
        )

    def connect(self) -> None:
        """Join the peer concurrently and warm both directions before serving."""
        try:
            self.t2d_comm = PyNcclCommunicator.from_unique_id_bytes(
                bytes.fromhex(self.unique_ids[0]), self.rank, 2, self.device
            )
            self.d2t_comm = PyNcclCommunicator.from_unique_id_bytes(
                bytes.fromhex(self.unique_ids[1]), self.rank, 2, self.device
            )
            # Native construction warms collectives. Match a P2P exchange too,
            # so the first request does not establish channels while awaiting
            # a later control operation from its peer.
            with torch.cuda.device(self.device), torch.cuda.stream(self.input_stream):
                value = torch.zeros(1, device=self.device)
                if self.rank == 0:
                    self.t2d_comm.send(value, self.remote_rank, self.input_stream)
                    self.d2t_comm.recv(value, self.remote_rank, self.input_stream)
                else:
                    self.t2d_comm.recv(value, self.remote_rank, self.input_stream)
                    self.d2t_comm.send(value, self.remote_rank, self.input_stream)
                self.input_stream.synchronize()
        except BaseException:
            self.close(abort=True)
            raise

    def send_features(
        self, features: torch.Tensor, ready: list[torch.cuda.Event], rows: int
    ) -> None:
        """Send each produced plane without making the host await its device event."""
        assert self.t2d_comm is not None
        with torch.cuda.device(self.device), torch.cuda.stream(self.input_stream):
            for plane, event in zip(features, ready, strict=True):
                self.input_stream.wait_event(event)
                self.t2d_comm.send(plane[:rows], self.remote_rank, self.input_stream)
            features.record_stream(self.input_stream)
            self.input_ready.record(self.input_stream)

    def receive_features(
        self, features: torch.Tensor, rows: int, consumer_stream: torch.cuda.Stream
    ) -> None:
        """Post the announced input shapes after prior consumers release the buffer."""
        assert self.t2d_comm is not None
        with torch.cuda.device(self.device), torch.cuda.stream(self.input_stream):
            self.input_stream.wait_stream(consumer_stream)
            for plane in features:
                self.t2d_comm.recv(plane[:rows], self.remote_rank, self.input_stream)
            self.input_ready.record(self.input_stream)

    def receive_output(
        self, tokens: torch.Tensor, probabilities: torch.Tensor | None
    ) -> None:
        """Post the result tensors on the independent return stream."""
        assert self.d2t_comm is not None
        self.output_stream.wait_stream(torch.cuda.current_stream(self.device))
        with torch.cuda.device(self.device), torch.cuda.stream(self.output_stream):
            self.d2t_comm.recv(tokens, self.remote_rank, self.output_stream)
            tokens.record_stream(self.output_stream)
            if probabilities is not None:
                self.d2t_comm.recv(probabilities, self.remote_rank, self.output_stream)
                probabilities.record_stream(self.output_stream)
            self.output_ready.record(self.output_stream)

    def send_device_inputs(self, packed: torch.Tensor, ready: torch.cuda.Event) -> None:
        """Send logical token and length inputs after their producing Target stream."""
        assert self.t2d_comm is not None
        with torch.cuda.device(self.device), torch.cuda.stream(self.input_stream):
            self.input_stream.wait_event(ready)
            self.t2d_comm.send(packed, self.remote_rank, self.input_stream)
            packed.record_stream(self.input_stream)
            self.input_ready.record(self.input_stream)

    def receive_device_inputs(self, packed: torch.Tensor) -> None:
        """Receive the reserved batch's logical inputs after its feature planes."""
        assert self.t2d_comm is not None
        with torch.cuda.device(self.device), torch.cuda.stream(self.input_stream):
            self.t2d_comm.recv(packed, self.remote_rank, self.input_stream)
            packed.record_stream(self.input_stream)
            self.input_ready.record(self.input_stream)

    def send_output(
        self,
        tokens: torch.Tensor,
        probabilities: torch.Tensor | None,
        producer_stream: torch.cuda.Stream,
    ) -> None:
        """Publish native candidate tensors and retain their producer allocations."""
        assert self.d2t_comm is not None
        with torch.cuda.device(self.device), torch.cuda.stream(self.output_stream):
            self.output_stream.wait_stream(producer_stream)
            self.d2t_comm.send(tokens, self.remote_rank, self.output_stream)
            tokens.record_stream(self.output_stream)
            if probabilities is not None:
                self.d2t_comm.send(probabilities, self.remote_rank, self.output_stream)
                probabilities.record_stream(self.output_stream)
            self.output_ready.record(self.output_stream)

    def close(self, *, abort: bool = False) -> None:
        """Drain normal IO or abort failed IO before releasing native communicators."""
        try:
            if not abort:
                self.input_stream.synchronize()
                self.output_stream.synchronize()
        finally:
            if self.d2t_comm is not None:
                self.d2t_comm.destroy()
                self.d2t_comm = None
            if self.t2d_comm is not None:
                self.t2d_comm.destroy()
                self.t2d_comm = None
