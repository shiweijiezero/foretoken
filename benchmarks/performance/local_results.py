# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""在一个确定目录中保存 HTTP 性能评测产物。"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Optional


class LocalResultDirectory:
    """拥有一次负载点或参数实验的本地 JSON 产物目录。"""

    def __init__(
        self,
        root_dir: str = "results",
        *,
        output_dir: Optional[str] = None,
        enabled: bool = True,
    ) -> None:
        if output_dir is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_dir = os.path.join(root_dir, timestamp)
        self.output_dir = output_dir
        self.enabled = enabled
        if enabled:
            os.makedirs(self.output_dir, exist_ok=True)

    def save_json(self, filename: str, data: Any) -> Optional[str]:
        """启用本地输出时写入一个 JSON 产物并返回路径。"""
        if not self.enabled:
            return None
        path = os.path.join(self.output_dir, filename)
        with open(path, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=4, ensure_ascii=False)
        return path
