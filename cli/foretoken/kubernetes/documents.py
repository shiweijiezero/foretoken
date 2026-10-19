# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Preserve Kubernetes scalar types across Python and Go manifest consumers."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from typing import Any

import yaml

try:
    from yaml import CSafeLoader as _SafeLoader
except ImportError:
    from yaml import SafeLoader as _SafeLoader


class _ResourceLoader(_SafeLoader):
    """Read Go-rendered resources without turning exponent numbers or timestamps into Python-only types."""


# Go accepts exponent numbers without a decimal point and keeps timestamps as strings.
# Register only on this loader; application configuration keeps its own YAML semantics.
_ResourceLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(r"^[+-]?(?:[0-9][0-9_]*(?:\.[0-9_]*)?|\.[0-9_]+)[eE][+-]?[0-9]+$"),
    list("-+0123456789."),
)
_ResourceLoader.add_constructor("tag:yaml.org,2002:timestamp", _SafeLoader.construct_scalar)


def decode_resources(text: str) -> Iterator[dict[str, Any]]:
    """Read a rendered Kubernetes resource stream using Go-compatible scalar interpretation."""
    yield from (value for value in yaml.load_all(text, Loader=_ResourceLoader) if value is not None)


def encode_resource(resource: dict[str, Any]) -> str:
    """Encode one YAML-derived resource with quoted strings and explicit non-string tags for kubectl."""
    return yaml.safe_dump(resource, sort_keys=False, default_style='"')


def encode_resources(resources: Iterable[dict[str, Any]]) -> str:
    """Encode a resource stream for Kustomize and kubectl without implicit scalar reinterpretation."""
    return yaml.safe_dump_all(resources, sort_keys=False, default_style='"')
