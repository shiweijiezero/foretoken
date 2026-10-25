# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Select the Draft adapter, then run the native headless EngineCore launcher."""

import os
from dataclasses import fields


def main():
    """Resolve Draft-specific configuration before native process and scheduler startup."""
    os.environ.setdefault("VLLM_USE_V2_MODEL_RUNNER", "1")

    from vllm.config import ModelConfig
    from vllm.engine.arg_utils import AsyncEngineArgs
    from vllm.entrypoints.cli.serve import run_headless
    from vllm.utils.argparse_utils import FlexibleArgumentParser

    from .protocol import Settings
    from .runner import replace_binding
    from .worker import validate_config

    parser = FlexibleArgumentParser(description=__doc__)
    AsyncEngineArgs.add_cli_args(parser)
    args = parser.parse_args()
    engine_args = AsyncEngineArgs.from_cli_args(args)
    engine_args.worker_cls = (
        "foretoken_dt.worker.MctracerDraftWorker"
        if engine_args.worker_cls == "foretoken_mctracer.Worker"
        else "foretoken_dt.worker.DraftWorker"
    )
    engine_args.skip_tokenizer_init = True
    prefix_caching = engine_args.enable_prefix_caching
    # Resolve neural drafts without skipping their Target feature inputs.
    # Ordinary drafts restore the native cache choice for their own engine.
    engine_args.enable_prefix_caching = False
    config = engine_args.create_engine_config(headless=True)
    if Settings.from_engine(config).role != "draft":
        parser.error(
            "the Draft launcher requires additional_config.foretoken_dt.role=draft"
        )
    validate_config(config)
    spec = config.speculative_config
    if spec is not None and not spec.uses_draft_model() and prefix_caching is True:
        parser.error("neural Drafts require --no-enable-prefix-caching")
    if spec is not None and spec.uses_draft_model():
        engine_args.enable_prefix_caching = prefix_caching
        # Reinitialize through native validation without copying derived caches
        # or materializing unset InitVars as explicit constructor arguments.
        draft_arguments = {
            field.name: getattr(spec.draft_model_config, field.name)
            for field in fields(spec.draft_model_config)
            if field.init
        }
        draft_arguments.update(runner="generate", skip_tokenizer_init=True)
        draft_model = ModelConfig(**draft_arguments)
        engine_args.speculative_config = None
        with (
            replace_binding(engine_args, "create_model_config", lambda: draft_model),
            replace_binding(
                engine_args, "create_speculative_config", lambda *args, **kwargs: None
            ),
        ):
            config = engine_args.create_engine_config(headless=True)
    scheduler = (
        "AsyncDraftScheduler"
        if config.scheduler_config.async_scheduling
        else "DraftScheduler"
    )
    config.scheduler_config.scheduler_cls = f"foretoken_dt.scheduler.{scheduler}"

    # The native launcher owns signals, Core processes, liveness and shutdown.
    # Its configuration factory consumes the already resolved Draft configuration.
    args.api_server_count = 0
    args.reasoning_parser_plugin = None
    with replace_binding(
        AsyncEngineArgs, "create_engine_config", lambda *args, **kwargs: config
    ):
        run_headless(args)


if __name__ == "__main__":
    main()
