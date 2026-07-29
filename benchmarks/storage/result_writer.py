# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Persist benchmark artifacts under a timestamped run directory."""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any


class ResultWriter:
    def __init__(self, root_dir: str = "results"):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir = os.path.join(root_dir, timestamp)
        os.makedirs(self.output_dir, exist_ok=True)

    def save_json(self, filename: str, data: Any) -> str:
        path = os.path.join(self.output_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        return path
