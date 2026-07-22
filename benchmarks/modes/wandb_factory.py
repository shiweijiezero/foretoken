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
"""Build ``WandbWriter`` instances from ``BenchArguments``."""

from __future__ import annotations

from typing import Any, Optional

from benchmarks.arguments import BenchArguments
from benchmarks.storage.wandb_writer import WandbWriter


def infer_api_type(url: str) -> Optional[str]:
    """Rough api type for EvalScope create_message field selection."""
    u = (url or "").lower()
    if "embedding" in u:
        return "openai_embedding"
    if "rerank" in u:
        return "openai_rerank"
    if "chat/completions" in u or "completions" in u:
        return "openai"
    return "openai"


class WandbFactory:
    """Create W&B writers with shared project / entity / api_type defaults."""

    def __init__(self, args: BenchArguments):
        self.args = args

    def create(
        self,
        config: dict[str, Any],
        wandb_dir: str = "",
        *,
        run_name: str = "",
        group: str = "",
        job_type: str = "",
    ) -> WandbWriter:
        args = self.args
        return WandbWriter(
            enabled=args.wandb,
            project=args.wandb_project or "foretoken-bench",
            entity=args.wandb_entity,
            run_name=run_name or args.wandb_run_name,
            config=config,
            wandb_dir=wandb_dir,
            api_type=infer_api_type(args.url),
            group=group,
            job_type=job_type,
        )
