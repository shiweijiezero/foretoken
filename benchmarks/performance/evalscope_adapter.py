# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""定义固定 EvalScope 参数与 OpenAI API 插件适配。"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from evalscope.perf.arguments import Arguments
from evalscope.perf.plugin.api.openai_api import OpenaiPlugin
from evalscope.perf.plugin.registry import register_api

EVALSCOPE_API = "foretoken_openai"


class ForetokenEvalScopeArguments(Arguments):
    """携带 Foretoken 请求字段省略语义的 EvalScope 参数。"""

    omit_temperature: bool = Field(default=False, exclude=True, repr=False)


@register_api(EVALSCOPE_API)
class ForetokenOpenaiPlugin(OpenaiPlugin):
    """复用 EvalScope OpenAI 执行，仅省略未配置的 temperature。"""

    def build_request(self, messages: Any, param: Any = None) -> dict[str, Any]:
        """构造 EvalScope 请求，并恢复 Foretoken 的可选 temperature 语义。"""
        request = super().build_request(messages, param)
        if self.param.omit_temperature:
            request.pop("temperature", None)
        return request
