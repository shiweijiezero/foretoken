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
"""Load benchmark request payloads from prompt, EvalScope, or JSONL."""

from __future__ import annotations

from typing import Optional

from benchmarks.arguments import BenchArguments


class RequestLoader:
    """Resolve ``--prompt`` / dataset plugins into a request list."""

    def __init__(self, args: BenchArguments):
        self.args = args

    def load(self) -> Optional[list[dict]]:
        args = self.args
        if args.prompt:
            return [
                {"prompt": args.prompt} for _ in range(args.primary_number)
            ]

        name = (args.dataset or "").strip()
        limit = max(args.number) if args.number else args.primary_number

        try:
            from benchmarks.workload.evalscope_loader import (
                EvalscopeDatasetLoader,
            )

            return EvalscopeDatasetLoader(args, limit=limit).load()
        except ImportError:
            from benchmarks.workload.jsonl_loader import JsonlPromptLoader

            jsonl_path = args.primary_dataset_path
            if not jsonl_path and name.endswith(".jsonl"):
                jsonl_path = name
            if not jsonl_path:
                raise
            return JsonlPromptLoader(jsonl_path, limit=limit).load()
