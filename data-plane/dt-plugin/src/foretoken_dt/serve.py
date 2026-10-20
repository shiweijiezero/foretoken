# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Launch an independent Draft or Target using the engine's own argument parser."""


def main() -> None:
    """Start one internal role service; vLLM owns model and accelerator settings."""
    import os

    import torch

    is_metax = bool(getattr(torch.version, "maca", None))
    # Engine images may restrict plugin discovery, including with an empty list.
    # A DT role owns this required plugin in its spawned engine processes.
    allowed_plugins = os.environ.get("VLLM_PLUGINS")
    if allowed_plugins is not None:
        plugins = [name for name in allowed_plugins.split(",") if name]
        required = (
            ("metax", "metax_enhanced_model", "metax_out_of_tree", "foretoken_dt")
            if is_metax
            else ("foretoken_dt",)
        )
        plugins.extend(name for name in required if name not in plugins)
        os.environ["VLLM_PLUGINS"] = ",".join(plugins)

    import uvicorn
    from vllm import AsyncEngineArgs
    from vllm.utils.argparse_utils import FlexibleArgumentParser

    from .service import create_app
    from .vllm import register
    from .vllm.config import ExternalEngineArgs

    parser = FlexibleArgumentParser(description=__doc__)
    AsyncEngineArgs.add_cli_args(parser)
    parser.set_defaults(enforce_eager=True, async_scheduling=False)
    parser.add_argument("--role", choices=("draft", "target"), required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19100)
    parser.add_argument(
        "--draft-tokens",
        type=int,
        help="Target's default candidates per round (default 3)",
    )
    parser.add_argument(
        "--max-candidate-tokens",
        type=int,
        help="independent Target's engine capacity (defaults to --draft-tokens)",
    )
    parser.add_argument(
        "--tensor-transport",
        choices=("auto", "gpu-ipc", "rdma", "zmq", "none"),
        default="auto",
        help="auto offers granted GPU peers, ZMQ and allocated RDMA; none selects token-only",
    )
    parser.add_argument(
        "--rdma-host", help="local routable address for allocated Mooncake RDMA"
    )
    parser.add_argument(
        "--zmq-endpoint",
        help="override the automatic TCP socket with tcp://host:port or shared ipc:///path",
    )
    parser.add_argument("--rdma-nic", default="", help="local RDMA device selection")
    parser.add_argument(
        "--enable-kv-migration",
        action="store_true",
        help="enable round-boundary Target KV checkpoints (NVIDIA, full attention)",
    )
    args = parser.parse_args()
    gpu_ipc_host = None
    if args.tensor_transport == "none":
        if args.rdma_host or args.rdma_nic or args.zmq_endpoint:
            parser.error("token-only mode does not use tensor transport endpoints")
    elif args.tensor_transport == "rdma":
        if not args.rdma_host or args.zmq_endpoint:
            parser.error("RDMA-only mode requires --rdma-host and no --zmq-endpoint")
    else:
        if args.tensor_transport == "zmq" and (args.rdma_host or args.rdma_nic):
            parser.error("ZMQ-only mode does not use RDMA options")
        if args.tensor_transport == "gpu-ipc" and (
            args.rdma_host or args.rdma_nic or args.zmq_endpoint
        ):
            parser.error("GPU IPC-only mode does not use RDMA or ZMQ options")
        if args.zmq_endpoint is None or args.tensor_transport == "auto":
            from vllm.utils.network_utils import get_ip

            # Managed roles inherit their Pod IP. Standalone roles reuse vLLM's
            # address selection when the HTTP listener binds all interfaces.
            address = (
                os.environ.get("FORETOKEN_MEMBER_IP") or args.rdma_host or args.host
            )
            if address in ("0.0.0.0", "::"):
                address = get_ip()
            if address in ("0.0.0.0", "::"):
                parser.error("tensor transport requires a reachable local address")
            host = f"[{address}]" if ":" in address else address
            if args.tensor_transport != "gpu-ipc" and args.zmq_endpoint is None:
                args.zmq_endpoint = f"tcp://{host}:*"
            if args.tensor_transport in ("gpu-ipc", "auto"):
                gpu_ipc_host = address
    tensor_enabled = bool(args.rdma_host or args.zmq_endpoint or gpu_ipc_host)
    if args.role == "draft" and (
        args.draft_tokens is not None or args.max_candidate_tokens is not None
    ):
        parser.error("--draft-tokens and --max-candidate-tokens are Target options")
    if any(
        value is not None and value <= 0
        for value in (args.draft_tokens, args.max_candidate_tokens)
    ):
        parser.error("candidate counts must be positive")
    default_draft_tokens = (
        (args.draft_tokens if args.draft_tokens is not None else 3)
        if args.role == "target"
        else None
    )
    engine_args = ExternalEngineArgs.from_cli_args(args)
    neural = engine_args.speculative_config is not None
    if any(
        value is not None
        for value in (
            engine_args.spec_method,
            engine_args.spec_model,
            engine_args.spec_tokens,
        )
    ):
        parser.error(
            "DT neural drafts use --speculative-config; independent Targets use --max-candidate-tokens"
        )
    if neural:
        if args.max_candidate_tokens is not None:
            parser.error(
                "neural engine capacity comes from speculative-config.num_speculative_tokens"
            )
        if not tensor_enabled:
            parser.error("neural Draft context requires tensor transport")
        engine_args.additional_config = {
            **(engine_args.additional_config or {}),
            "foretoken_dt_context_role": args.role,
        }
        config = engine_args.speculative_config
        if config.setdefault("draft_sample_method", "probabilistic") != "probabilistic":
            parser.error("neural DT requires draft_sample_method=probabilistic")
    if (
        engine_args.worker_cls not in ("auto", "foretoken_mctracer.Worker")
        or engine_args.scheduler_cls is not None
    ):
        parser.error("DT roles own --worker-cls and --scheduler-cls")
    if args.rdma_nic and not args.rdma_host:
        parser.error("--rdma-nic requires --rdma-host")
    if args.enable_kv_migration:
        from vllm.config import KVTransferConfig

        if args.role != "target" or not args.rdma_host or is_metax:
            parser.error("KV migration requires an NVIDIA Target with RDMA")
        if args.tensor_transport != "rdma":
            parser.error("KV migration requires --tensor-transport rdma")
        if engine_args.kv_transfer_config is not None:
            parser.error("DT migration owns the KV connector")
        engine_args.kv_transfer_config = KVTransferConfig(
            kv_connector="DraftTargetKvConnector",
            kv_role="kv_both",
            kv_connector_module_path="foretoken_dt.vllm.migration",
        )
    if engine_args.async_scheduling:
        parser.error("DT requires --no-async-scheduling")
    register()
    if tensor_enabled:
        if engine_args.worker_extension_cls:
            parser.error("tensor transport roles own --worker-extension-cls")
        engine_args.worker_extension_cls = (
            "foretoken_dt.worker.DraftTargetWorkerExtension"
        )
    if args.role == "target" and not neural:
        # Native rejection sampling verifies candidates from the remote Draft.
        engine_args.speculative_config = {
            "method": "external",
            "num_speculative_tokens": args.max_candidate_tokens or default_draft_tokens,
            "draft_sample_method": "probabilistic" if tensor_enabled else "greedy",
        }
    uvicorn.run(
        create_app(
            engine_args,
            args.role,
            default_draft_tokens,
            args.rdma_host,
            args.rdma_nic,
            args.zmq_endpoint,
            gpu_ipc_host,
        ),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
