# Copyright 2026 Foretoken contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Benchmark entry: validate args and dispatch to the selected mode."""

from __future__ import annotations

from typing import Any

from benchmarks.arguments import BenchArguments
from benchmarks.modes import SingleMode, SweepMode
from benchmarks.modes.requests import RequestLoader


async def run_benchmark(args: BenchArguments) -> dict[str, Any]:
    """Validate, load requests, and run single / sweep / param-sweep."""
    args.validate()

    if args.is_param_sweep:
        from benchmarks.sweep.param_runner import run_param_sweep

        return await run_param_sweep(args)

    requests = RequestLoader(args).load()
    if args.is_sweep:
        return await SweepMode(args, requests).run()
    return await SingleMode(args, requests).run()
