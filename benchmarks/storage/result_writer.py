# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Persist benchmark artifacts under a timestamped run directory."""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Optional


class ResultWriter:
    def __init__(
        self,
        root_dir: str = "results",
        *,
        output_dir: Optional[str] = None,
        enabled: bool = True,
    ):
        if output_dir is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_dir = os.path.join(root_dir, timestamp)
        self.output_dir = output_dir
        self.enabled = enabled
        if enabled:
            os.makedirs(self.output_dir, exist_ok=True)

    def child(self, name: str) -> "ResultWriter":
        """Return a writer rooted at ``output_dir/name``."""
        return ResultWriter(
            output_dir=os.path.join(self.output_dir, name),
            enabled=self.enabled,
        )

    def save_json(self, filename: str, data: Any) -> Optional[str]:
        if not self.enabled:
            return None
        path = os.path.join(self.output_dir, filename)
        with open(path, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=4, ensure_ascii=False)
        return path
