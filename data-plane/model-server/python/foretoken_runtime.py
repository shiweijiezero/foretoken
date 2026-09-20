# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Shared socket lifecycle for private inference-engine observation adapters."""

import threading
from collections.abc import Callable

import zmq


class PrefixLookupServer:
    """Own one read-only JSON request socket until its engine worker shuts down."""

    def __init__(self, endpoint: str, lookup: Callable[[dict], dict]):
        self._endpoint = endpoint
        self._lookup = lookup
        self._stopping = threading.Event()
        self._context = zmq.Context()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        with self._context.socket(zmq.REP) as socket:
            socket.setsockopt(zmq.LINGER, 0)
            socket.bind(self._endpoint)
            while not self._stopping.is_set():
                if not socket.poll(100):
                    continue
                socket.send_json(self._lookup(socket.recv_json()))

    def close(self) -> None:
        """Stops the worker thread and releases its socket context."""
        self._stopping.set()
        self._thread.join()
        self._context.term()


def offset_tcp_endpoint(endpoint: str, offset: int) -> str:
    """Offsets the port in a controller-owned TCP endpoint without changing its host."""
    prefix, separator, raw_port = endpoint.rpartition(":")
    if not separator or not prefix.startswith("tcp://"):
        raise ValueError("shared KV lookup endpoint must be a TCP endpoint")
    port = int(raw_port) + offset
    if port < 1 or port > 65535:
        raise ValueError("shared KV lookup endpoint port is outside the TCP range")
    return f"{prefix}:{port}"
