# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Strict scalar decoders shared by external HTTP and video sweep inputs."""

from pydantic import (
    ConfigDict,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    TypeAdapter,
)

_ERRORS = ConfigDict(hide_input_in_errors=True)
INTEGER = TypeAdapter(StrictInt, config=_ERRORS)
NUMBER = TypeAdapter(StrictFloat, config=_ERRORS)
BOOLEAN = TypeAdapter(StrictBool, config=_ERRORS)
STRING = TypeAdapter(StrictStr, config=_ERRORS)
OPTIONAL_INTEGER = TypeAdapter(StrictInt | None, config=_ERRORS)
OPTIONAL_NUMBER = TypeAdapter(StrictFloat | None, config=_ERRORS)
