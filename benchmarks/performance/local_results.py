# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Save HTTP benchmark artifacts in a designated directory."""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Optional


class LocalResultDirectory:
    """Own the local JSON artifact directory for one workload point or parameter experiment."""

    def __init__(
        self,
        root_dir: str | None = None,
        *,
        output_dir: Optional[str] = None,
        enabled: bool = True,
    ) -> None:
        if output_dir is None:
            if root_dir is None:
                raise ValueError("root_dir is required when output_dir is omitted")
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_dir = os.path.join(root_dir, timestamp)
        self.output_dir = output_dir
        self.enabled = enabled
        if enabled:
            os.makedirs(self.output_dir, exist_ok=True)

    def save_json(self, filename: str, data: Any) -> Optional[str]:
        """Write a JSON artifact and return its path when local output is enabled."""
        if not self.enabled:
            return None
        path = os.path.join(self.output_dir, filename)
        with open(path, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=4, ensure_ascii=False)
        return path
