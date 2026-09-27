# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Cross-host tensor and buffer-reuse diagnostic for the DT transport."""

import argparse
import asyncio
import json
import time


async def _send(writer: asyncio.StreamWriter, message: dict) -> None:
    """Send one control message; tensor contents never enter this socket."""
    writer.write(json.dumps(message).encode() + b"\n")
    await writer.drain()


async def _receive(reader: asyncio.StreamReader) -> dict:
    """Read one control message, exposing disconnects instead of freeing exports."""
    line = await reader.readline()
    if not line:
        raise ConnectionError("peer disconnected before the transfer acknowledgement")
    message = json.loads(line)
    if not isinstance(message, dict):
        raise TypeError("control message must be a JSON object")
    return message


def _ready_event(tensor):
    """Record the diagnostic's single-stream tensor use for transport ownership."""
    import torch

    if not tensor.is_cuda:
        return None
    event = torch.cuda.Event()
    event.record(torch.cuda.current_stream(tensor.device))
    return event


async def _serve(args: argparse.Namespace) -> None:
    """Reuse one registration, changing its contents only after each read ACK."""
    import torch

    from .transport import MooncakeTransport

    transport = MooncakeTransport(args.host, args.nic)
    tensor = torch.arange(args.elements, dtype=torch.int64, device=args.device)
    buffer = transport.register(tensor)
    completed = asyncio.get_running_loop().create_future()
    claimed = False

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        nonlocal claimed
        if claimed:
            writer.close()
            await writer.wait_closed()
            return
        claimed = True
        try:
            for iteration in range(args.iterations):
                if iteration:
                    tensor.add_(1)
                source = await buffer.publish(ready_event=_ready_event(tensor))
                await _send(
                    writer,
                    {
                        "version": 1,
                        "iteration": iteration,
                        "iterations": args.iterations,
                        "payload": source.to_wire(),
                    },
                )
                message = await _receive(reader)
                if message.get("op") != "read_complete":
                    raise ValueError("expected a completed-read acknowledgement")
                buffer.release_source(message["publication_id"])
                await _send(writer, {"op": "released"})
            await buffer.close(ready_event=_ready_event(tensor))
            completed.set_result(None)
        except (TypeError, ValueError, KeyError, RuntimeError, OSError) as error:
            # This diagnostic owns a single session: propagate its failure to the
            # process. Never release an exported tensor just because TCP closed.
            completed.set_exception(error)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, args.host, args.port)
    async with server:
        print(
            json.dumps(
                {"listening": [args.host, args.port], "segment": transport.segment}
            ),
            flush=True,
        )
        await completed
    await transport.close()


async def _read(args: argparse.Namespace) -> None:
    """Read into local device storage, acknowledge the copy, then verify its contents."""
    import torch

    from .transport import MooncakeTransport, PayloadRef

    transport = MooncakeTransport(args.host, args.nic)
    reader, writer = await asyncio.open_connection(args.peer, args.port)
    try:
        message = await _receive(reader)
        if message["version"] != 1:
            raise ValueError("unsupported diagnostic protocol version")
        iterations = message["iterations"]
        if type(iterations) is not int or iterations <= 0:
            raise ValueError("iterations must be a positive integer")
        source = PayloadRef.from_wire(message["payload"])
        if source.nbytes % 8:
            raise ValueError("diagnostic payload must contain whole int64 elements")
        elements = source.nbytes // 8
        tensor = torch.empty(elements, dtype=torch.int64, device=args.device)
        buffer = transport.register(tensor)
        expected = torch.arange(elements, dtype=torch.int64, device=args.device)
        elapsed = 0.0
        for iteration in range(iterations):
            if iteration:
                message = await _receive(reader)
                expected.add_(1)
            if message["iteration"] != iteration or message["iterations"] != iterations:
                raise ValueError("publication sequence changed during the diagnostic")
            source = PayloadRef.from_wire(message["payload"])
            start = time.perf_counter()
            await buffer.read(source, ready_event=_ready_event(tensor))
            elapsed += time.perf_counter() - start
            await _send(
                writer,
                {"op": "read_complete", "publication_id": source.publication_id},
            )
            if (await _receive(reader)).get("op") != "released":
                raise ValueError("producer did not confirm source release")
            if not torch.equal(tensor, expected):
                await transport.close()
                raise RuntimeError(f"RDMA payload differs at iteration {iteration}")
        await buffer.close(ready_event=_ready_event(tensor))
        await transport.close()
        print(
            json.dumps(
                {
                    "verified": True,
                    "requested_transport": "rdma",
                    "device": str(tensor.device),
                    "bytes": source.nbytes,
                    "iterations": iterations,
                    "transfer_seconds": elapsed,
                }
            ),
            flush=True,
        )
    finally:
        writer.close()
        await writer.wait_closed()


def main() -> None:
    """Run a producer or consumer diagnostic inside an existing engine environment."""
    parser = argparse.ArgumentParser(
        description="Validate DT tensor transfers through Mooncake RDMA (not inference)."
    )
    subparsers = parser.add_subparsers(dest="role", required=True)
    for role in ("serve", "read"):
        command = subparsers.add_parser(role)
        command.add_argument(
            "--host", required=True, help="Routable local host/IP, no port"
        )
        command.add_argument("--port", type=int, default=19090, help="Control TCP port")
        command.add_argument(
            "--nic", default="", help="Optional Mooncake RDMA device filter"
        )
        command.add_argument(
            "--device", default="cuda:0", help="Torch device; CPU is diagnostic only"
        )
        if role == "serve":
            command.add_argument("--elements", type=int, default=1048576)
            command.add_argument("--iterations", type=int, default=1)
        else:
            command.add_argument(
                "--peer", required=True, help="Producer's control host/IP"
            )
    args = parser.parse_args()
    if args.role == "serve" and (args.elements <= 0 or args.iterations <= 0):
        parser.error("--elements and --iterations must be positive")
    asyncio.run(_serve(args) if args.role == "serve" else _read(args))


if __name__ == "__main__":
    main()
