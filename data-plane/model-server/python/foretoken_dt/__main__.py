# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Run the Draft half of one synchronous EAGLE session."""

import argparse
import logging
import os


def serve(endpoint: str, kv_cache_memory_bytes: int | None) -> None:
    """Own one Draft CUDA runtime and serve ordered proposals until interrupted."""
    os.environ["VLLM_USE_V2_MODEL_RUNNER"] = "1"
    from vllm.config import set_current_vllm_config
    from vllm.engine.arg_utils import EngineArgs

    from foretoken_dt.draft import DraftRuntime
    from foretoken_dt.transport import Server

    logger = logging.getLogger(__name__)
    runtime = None
    config = None
    with Server(endpoint, device="cuda:0") as server:
        logger.info(
            "Draft listening on %s; waiting for Target initialization", endpoint
        )
        try:
            while True:
                message = server.receive()
                operation = message.metadata["operation"]
                output = None
                try:
                    if operation == "init":
                        if runtime is not None:
                            raise ValueError(
                                "Draft already has a Target; restart it for a new session"
                            )
                        config = EngineArgs(
                            **message.metadata["engine_args"]
                        ).create_engine_config()
                        with set_current_vllm_config(config):
                            runtime = DraftRuntime(
                                config, kv_cache_memory_bytes=kv_cache_memory_bytes
                            )
                            runtime.bootstrap(
                                message.tensors,
                                message.metadata["aux_hidden_state_count"],
                            )
                    else:
                        if runtime is None:
                            raise ValueError(
                                "Target must initialize Draft before sending work"
                            )
                        with set_current_vllm_config(config):
                            if operation in ("propose", "prefill"):
                                output = runtime.propose(
                                    message.metadata,
                                    message.tensors,
                                    message.feedback_ready,
                                    message.hidden_ready,
                                )
                            elif operation == "free":
                                runtime.free(message.metadata["req_ids"])
                            else:
                                raise ValueError(f"Unknown DT operation: {operation}")
                except Exception as error:
                    # A failed proposal can have partially modified KV state. Report
                    # the failure, then end the session instead of reusing that state.
                    server.reply_error(error)
                    raise
                # The transport reports send failures through the error listener.
                # An ordinary reply may already have published its control header.
                server.reply(
                    {"status": "ready" if operation == "init" else "ok"}, output
                )
                if operation == "init":
                    logger.info("Draft model and KV cache ready")
                # All consumers are queued on the runtime stream. Drop this
                # transaction before the next receive reuses its input storage.
                del message, output
        finally:
            # Retire DT communication before the runtime tears down its own
            # independent vLLM process groups and CUDA model resources.
            server.close()
            if runtime is not None:
                runtime.close()


def main() -> None:
    """Parse the Draft service's transport and GPU-memory options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--listen", required=True, help="ZeroMQ endpoint, e.g. tcp://127.0.0.1:5557"
    )
    parser.add_argument(
        "--kv-cache-memory-bytes",
        type=int,
        help="Draft KV pool bytes; otherwise derive from available GPU memory",
    )
    args = parser.parse_args()
    if args.kv_cache_memory_bytes is not None and args.kv_cache_memory_bytes <= 0:
        parser.error("--kv-cache-memory-bytes must be positive")
    logging.basicConfig(level=logging.INFO)
    try:
        serve(args.listen, args.kv_cache_memory_bytes)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
