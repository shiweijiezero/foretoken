# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Launch an independent Draft or Target using the engine's own argument parser."""


def main() -> None:
    """Start one internal role service; vLLM owns model and accelerator settings."""
    import os

    # Verification changes batch shapes. Select vLLM's invariant kernels before
    # importing engine modules to stabilize greedy decisions across those shapes.
    os.environ.setdefault("VLLM_BATCH_INVARIANT", "1")

    import uvicorn
    from vllm import AsyncEngineArgs
    from vllm.utils.argparse_utils import FlexibleArgumentParser

    from .service import create_app

    parser = FlexibleArgumentParser(description=__doc__)
    AsyncEngineArgs.add_cli_args(parser)
    parser.add_argument("--role", choices=("draft", "target"), required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19100)
    parser.add_argument("--draft-token-budget", type=int, default=3)
    parser.add_argument("--rdma-host", help="local routable address for Mooncake")
    parser.add_argument("--rdma-nic", default="", help="local RDMA device selection")
    args = parser.parse_args()
    if args.draft_token_budget <= 0:
        parser.error("--draft-token-budget must be positive")
    engine_args = AsyncEngineArgs.from_cli_args(args)
    if engine_args.speculative_config is not None:
        parser.error("the role owns speculative_config; use --draft-token-budget")
    if args.rdma_nic and not args.rdma_host:
        parser.error("--rdma-nic requires --rdma-host")
    if args.rdma_host:
        if (
            engine_args.tensor_parallel_size != 1
            or engine_args.pipeline_parallel_size != 1
            or engine_args.data_parallel_size != 1
        ):
            parser.error("RDMA roles currently require one local worker")
        if engine_args.worker_extension_cls:
            parser.error("RDMA roles own --worker-extension-cls")
        engine_args.worker_extension_cls = (
            "foretoken_dt.worker.DraftTargetWorkerExtension"
        )
        engine_args.enforce_eager = True
        engine_args.async_scheduling = False
    if args.role == "target":
        # The external-candidate engine extension owns verification and stopping.
        engine_args.speculative_config = {
            "method": "external",
            "num_speculative_tokens": args.draft_token_budget,
            "draft_sample_method": "probabilistic" if args.rdma_host else "greedy",
        }
        engine_args.enforce_eager = True
        engine_args.async_scheduling = False
    uvicorn.run(
        create_app(
            engine_args,
            args.role,
            args.draft_token_budget,
            args.rdma_host,
            args.rdma_nic,
        ),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
