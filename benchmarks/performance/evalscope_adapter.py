# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Define fixed EvalScope arguments and the OpenAI API plugin adapter."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from evalscope.perf.arguments import Arguments
from evalscope.perf.plugin.api.openai_api import OpenaiPlugin
from evalscope.perf.plugin.registry import register_api

EVALSCOPE_API = "foretoken_openai"


class ForetokenEvalScopeArguments(Arguments):
    """EvalScope arguments carrying Foretoken request-field omission semantics."""

    omit_temperature: bool = Field(default=False, exclude=True, repr=False)


@register_api(EVALSCOPE_API)
class ForetokenOpenaiPlugin(OpenaiPlugin):
    """Reuse EvalScope OpenAI execution while omitting an unset temperature."""

    def build_request(self, messages: Any, param: Any = None) -> dict[str, Any]:
        """Build an EvalScope request and restore Foretoken optional-temperature semantics."""
        request = super().build_request(messages, param)
        if self.param.omit_temperature:
            request.pop("temperature", None)
        return request
