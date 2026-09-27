# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Launch an independent Draft or Target using the engine's own argument parser."""


def main() -> None:
    """Start one internal role service; vLLM owns model and accelerator settings."""
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
    args = parser.parse_args()
    if args.draft_token_budget <= 0:
        parser.error("--draft-token-budget must be positive")
    engine_args = AsyncEngineArgs.from_cli_args(args)
    if engine_args.speculative_config is not None:
        parser.error("the role owns speculative_config; use --draft-token-budget")
    if args.role == "target":
        # These are the verified capabilities of the independent engine extension.
        engine_args.speculative_config = {
            "method": "external",
            "num_speculative_tokens": args.draft_token_budget,
        }
        engine_args.enforce_eager = True
        engine_args.async_scheduling = False
    uvicorn.run(
        create_app(engine_args, args.role, args.draft_token_budget),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
