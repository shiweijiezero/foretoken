# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Tensor transfers through ZMQ sockets and explicit host staging."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import torch
import zmq
import zmq.asyncio

from .transport import PayloadRef, TensorTransport


class ZmqTransport(TensorTransport):
    """Own one socket server and immutable host snapshots until Worker ACK or revoke."""

    def __init__(self, endpoint: str):
        """Reserve an IPC or TCP endpoint; start binds it on the Worker IO thread."""
        self.path = None
        if endpoint.startswith("ipc://"):
            self.path = Path(endpoint.removeprefix("ipc://"))
            if not self.path.is_absolute() or self.path.exists():
                raise ValueError("ZMQ requires a unique absolute IPC socket path")
        elif not endpoint.startswith("tcp://"):
            raise ValueError("ZMQ requires an ipc:// or tcp:// endpoint")
        elif urlsplit(endpoint).hostname in (None, "*", "0.0.0.0", "::"):
            raise ValueError("ZMQ TCP requires a reachable bind address")
        self.segment = endpoint
        self.publications: dict[str, tuple[PayloadRef, torch.Tensor]] = {}
        self.context: zmq.asyncio.Context | None = None
        self.socket: zmq.asyncio.Socket | None = None
        self.server: asyncio.Task | None = None
        super().__init__()

    async def start(self) -> None:
        """Bind on the Worker IO loop before the role advertises readiness."""
        self.context = zmq.asyncio.Context()
        self.socket = self.context.socket(zmq.ROUTER)
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.setsockopt(zmq.IPV6, int(self.segment.startswith("tcp://[")))
        self.socket.bind(self.segment)
        self.segment = self.socket.getsockopt_string(zmq.LAST_ENDPOINT)
        self.server = asyncio.create_task(self._serve())

    async def publish(self, tensor: torch.Tensor) -> PayloadRef:
        """Snapshot fenced GPU data on the host; keep it immutable until source release."""
        host = await asyncio.to_thread(tensor.detach().to, device="cpu", copy=True)
        payload = PayloadRef(
            uuid4().hex,
            self.segment,
            None,
            tensor.nbytes,
            str(tensor.dtype).removeprefix("torch."),
            tuple(tensor.shape),
            "zmq",
        )
        self.publications[payload.publication_id] = (payload, host)
        return payload

    async def _serve(self) -> None:
        """Serve independent claimed publications without a batch readiness barrier."""
        while True:
            identity, request = await self.socket.recv_multipart()
            descriptor = json.loads(request)
            publication = self.publications.get(descriptor["publication_id"])
            if publication is None or descriptor != publication[0].to_wire():
                await self.socket.send_multipart(
                    [identity, b"error", b"publication is unavailable or changed"]
                )
                continue
            # copy=True gives libzmq its own bytes before returning. Source ACK
            # may then release this host snapshot without a pending zero-copy send.
            host = publication[1]
            raw = memoryview(host.reshape(-1).view(torch.uint8).numpy()).cast("B")
            await self.socket.send_multipart([identity, b"ok", raw], copy=True)
            del publication, host, raw

    async def read(self, tensor: torch.Tensor, source: PayloadRef) -> None:
        """Pull a claimed host snapshot and finish H2D before native admission."""
        if source.transport != "zmq":
            raise ValueError("ZMQ requires a ZMQ publication")
        socket = self.context.socket(zmq.DEALER)
        socket.setsockopt(zmq.LINGER, 0)
        socket.setsockopt(zmq.IPV6, int(source.segment.startswith("tcp://[")))
        try:
            socket.connect(source.segment)
            await socket.send(json.dumps(source.to_wire()).encode())
            status, data = await socket.recv_multipart()
            if status != b"ok":
                raise RuntimeError(data.decode())
            if len(data) != source.nbytes:
                raise ValueError("ZMQ tensor byte size does not match publication")
            host = torch.frombuffer(bytearray(data), dtype=tensor.dtype).reshape(
                source.shape
            )
            destination = tensor[: source.shape[0]] if tensor.ndim else tensor
            # This blocking copy runs off the IO loop. TensorBuffer shields its
            # task from caller cancellation and retains both allocations until done.
            await asyncio.to_thread(destination.copy_, host, non_blocking=False)
        finally:
            socket.close()

    def release(self, publication_id: str) -> None:
        """Drop only a snapshot whose Worker has received ACK or proved no claim."""
        del self.publications[publication_id]

    async def shutdown(self) -> None:
        """Stop the socket after all transfers and publications have drained."""
        if self.server is not None:
            self.server.cancel()
            try:
                await self.server
            except asyncio.CancelledError:
                pass
        if self.socket is not None:
            self.socket.close()
            if self.path is not None:
                self.path.unlink()
        if self.context is not None:
            self.context.term()
