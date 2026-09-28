# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Launch an independent Draft or Target using the engine's own argument parser."""


def main() -> None:
    """Start one internal role service; vLLM owns model and accelerator settings."""
    import os

    # Verification changes batch shapes. Select vLLM's invariant kernels before
    # importing engine modules to stabilize greedy decisions across those shapes.
    os.environ.setdefault("VLLM_BATCH_INVARIANT", "1")
    # Engine images may restrict plugin discovery, including with an empty list.
    # A DT role owns this required plugin in its spawned engine processes.
    allowed_plugins = os.environ.get("VLLM_PLUGINS")
    if allowed_plugins is not None:
        plugins = [name for name in allowed_plugins.split(",") if name]
        if "foretoken_dt" not in plugins:
            os.environ["VLLM_PLUGINS"] = ",".join([*plugins, "foretoken_dt"])

    import uvicorn
    from vllm import AsyncEngineArgs
    from vllm.utils.argparse_utils import FlexibleArgumentParser

    from .service import create_app
    from .vllm import register
    from .vllm.config import ExternalEngineArgs

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
    engine_args = ExternalEngineArgs.from_cli_args(args)
    if any(
        value is not None
        for value in (
            engine_args.speculative_config,
            engine_args.spec_method,
            engine_args.spec_model,
            engine_args.spec_tokens,
        )
    ):
        parser.error("the role owns speculative_config; use --draft-token-budget")
    if engine_args.worker_cls != "auto" or engine_args.scheduler_cls is not None:
        parser.error("DT roles own --worker-cls and --scheduler-cls")
    if args.rdma_nic and not args.rdma_host:
        parser.error("--rdma-nic requires --rdma-host")
    engine_args.enforce_eager = True
    engine_args.async_scheduling = False
    register()
    if args.rdma_host:
        if engine_args.worker_extension_cls:
            parser.error("RDMA roles own --worker-extension-cls")
        engine_args.worker_extension_cls = (
            "foretoken_dt.worker.DraftTargetWorkerExtension"
        )
    if args.role == "target":
        # Native rejection sampling verifies candidates from the remote Draft.
        engine_args.speculative_config = {
            "method": "external",
            "num_speculative_tokens": args.draft_token_budget,
            "draft_sample_method": "probabilistic" if args.rdma_host else "greedy",
        }
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
