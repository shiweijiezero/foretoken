# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Preserve Kubernetes scalar types across Python and Go manifest consumers."""

from __future__ import annotations

import base64
import calendar
import math
import re
from collections.abc import Iterable, Iterator
from typing import Any, ClassVar

import numpy as np
import yaml
from yaml.constructor import ConstructorError

try:
    from yaml import CSafeLoader as _SafeLoader
except ImportError:
    from yaml import SafeLoader as _SafeLoader


class _ResourceLoader(_SafeLoader):
    """Read resource scalars and mapping keys with Kubernetes YAML-to-JSON semantics."""

    _booleans: ClassVar[dict[str, bool]] = {
        word: value
        for value, words in (
            (True, ("y", "Y", "yes", "Yes", "YES", "true", "True", "TRUE", "on", "On", "ON")),
            (False, ("n", "N", "no", "No", "NO", "false", "False", "FALSE", "off", "Off", "OFF")),
        )
        for word in words
    }
    _nulls = frozenset(("", "~", "null", "Null", "NULL"))
    _nonfinite: ClassVar[dict[str, float]] = {
        word: value
        for value, words in (
            (math.inf, (".inf", ".Inf", ".INF", "+.inf", "+.Inf", "+.INF")),
            (-math.inf, ("-.inf", "-.Inf", "-.INF")),
            (math.nan, (".nan", ".NaN", ".NAN")),
        )
        for word in words
    }
    _integer = re.compile(r"[+-]?(?:0[bB][01]+|0[oO][0-7]+|0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)")
    _decimal = re.compile(r"[+-]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][+-]?[0-9]+)?")
    _dot_float = re.compile(r"\.[0-9](?:_?[0-9])*(?:[eE][+-]?[0-9](?:_?[0-9])*)?")
    _timestamp = re.compile(
        r"(?P<year>[0-9]{4})-(?P<month>[0-9]{1,2})-(?P<day>[0-9]{1,2})"
        r"(?:(?P<separator>[Tt]| +)(?P<hour>[0-9]{1,2}):(?P<minute>[0-9]{1,2}):"
        r"(?P<second>[0-9]{1,2})(?:[.,][0-9]+)?(?P<zone>Z|[+-][0-9]{2}:[0-9]{2})?)?"
    )
    _scalar_tags = frozenset("tag:yaml.org,2002:" + kind for kind in ("bool", "null", "int", "float", "timestamp", "binary"))

    @classmethod
    def _scalar_value(cls, value: str) -> tuple[str, Any]:
        """Classify plain scalars in Go's int64, uint64, float64, then string order."""
        if value in cls._booleans:
            return "bool", cls._booleans[value]
        if value in cls._nulls:
            return "null", None
        if value in cls._nonfinite:
            return "float", cls._nonfinite[value]
        if value[0] not in "+-.0123456789":
            return "str", value
        # Go removes underscores on its digit/sign path, but a leading decimal
        # point goes directly through the floating-point literal grammar.
        plain = value if value[0] == "." else value.replace("_", "")
        if cls._integer.fullmatch(plain):
            digits = plain.lstrip("+-")
            base = 0 if digits[:2].lower() in {"0b", "0o", "0x"} else 8 if digits.startswith("0") else 10
            try:
                number = int(plain, base)
            except ValueError:
                # Python's decimal-string conversion budget does not make a Go
                # integer overflow invalid YAML; continue through float/string.
                pass
            else:
                if -(1 << 63) <= number < (1 << 63) or (plain[0] not in "+-" and 0 <= number < (1 << 64)):
                    return "int", number
        pattern = cls._dot_float if value[0] == "." else cls._decimal
        if pattern.fullmatch(plain):
            number = float(plain)
            if math.isfinite(number):
                return "float", number
        # Go also parses the payload of its legacy binary prefix independently,
        # after base-0 integers and decimals, including a sign inside that prefix.
        if plain.startswith("0b"):
            binary = plain[2:]
        elif plain.startswith("-0b"):
            binary = "-" + plain[3:]
        else:
            return "str", value
        if re.fullmatch(r"[+-]?[01]+", binary):
            number = int(binary, 2)
            if -(1 << 63) <= number < (1 << 63) or (binary[0] not in "+-" and 0 <= number < (1 << 64)):
                return "int", number
        return "str", value

    def resolve(self, kind: type, value: str, implicit: tuple[bool, bool]) -> str:
        """Resolve only plain resource scalars here; keep quoting, aliases and merge handling in PyYAML."""
        if kind is yaml.ScalarNode and implicit[0] and value != "<<":
            scalar_kind, _ = self._scalar_value(value)
            return "tag:yaml.org,2002:" + scalar_kind
        return super().resolve(kind, value, implicit)

    @classmethod
    def _valid_timestamp(cls, value: str) -> bool:
        """Validate the timestamp forms Go accepts without constructing a Python date or datetime."""
        match = cls._timestamp.fullmatch(value)
        if match is None:
            return False
        year, month, day = (int(match[name]) for name in ("year", "month", "day"))
        if not (1 <= month <= 12 and 1 <= day <= calendar.monthrange(year, month)[1]):
            return False
        if match["hour"] is None:
            return True
        zone = match["zone"]
        if (match["separator"] in {"T", "t"}) != bool(zone):
            return False
        if int(match["hour"]) >= 24 or int(match["minute"]) >= 60 or int(match["second"]) >= 60:
            return False
        return not zone or zone == "Z" or (int(zone[1:3]) <= 24 and int(zone[4:]) <= 60)

    def _construct_scalar(self, node: yaml.ScalarNode, *, key: bool = False) -> Any:
        """Construct standard scalar tags and reject tag mismatches or non-JSON numeric values."""
        value = self.construct_scalar(node)
        expected = node.tag.rsplit(":", 1)[-1]
        if expected == "binary":
            try:
                binary = base64.b64decode(value.replace("\r", "").replace("\n", ""), validate=True)
            except ValueError as error:
                raise ConstructorError(None, None, "invalid base64 scalar", node.start_mark) from error
            # Go's JSON encoder replaces each invalid UTF-8 byte separately.
            text = binary.decode("utf-8", "surrogateescape")
            return text.translate({code: 0xFFFD for code in range(0xDC80, 0xDD00)})
        if expected == "timestamp":
            if self._valid_timestamp(value):
                return value
        else:
            kind, result = self._scalar_value(value)
            if expected == "float" and kind == "int" and -(1 << 63) <= result < (1 << 63):
                kind, result = "float", float(result)
            if kind == expected:
                if key or kind != "float" or math.isfinite(result):
                    return result
                raise ConstructorError(None, None, "non-finite numbers cannot be Kubernetes JSON values", node.start_mark)
        raise ConstructorError(None, None, f"cannot decode {value!r} as !!{expected}", node.start_mark)

    @staticmethod
    def _float_key(value: float) -> str:
        """Format floating mapping keys using Kubernetes' shortest float32 decimal representation."""
        with np.errstate(over="ignore"):
            rounded = np.float32(value)
        if math.isnan(rounded):
            return ".nan"
        if math.isinf(rounded):
            return "-.inf" if rounded < 0 else ".inf"
        scientific = np.format_float_scientific(rounded, unique=True, trim="-", exp_digits=2)
        # Go's 'g' threshold follows the shortest decimal exponent, not the
        # binary value's position relative to a decimal power such as 1e-4.
        exponent = int(scientific.rsplit("e", 1)[1])
        if exponent < -4 or exponent >= 6:
            return scientific
        return np.format_float_positional(rounded, unique=True, trim="-")

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[str, Any]:
        """Normalize scalar keys before insertion so Python's bool/int equality cannot discard JSON fields."""
        self.flatten_mapping(node)
        mapping = {}
        for key_node, value_node in node.value:
            if not isinstance(key_node, yaml.ScalarNode):
                raise ConstructorError(None, None, "Kubernetes JSON object keys must be scalars", key_node.start_mark)
            key = (
                self._construct_scalar(key_node, key=True)
                if key_node.tag in self._scalar_tags else self.construct_object(key_node, deep=deep)
            )
            if isinstance(key, bool):
                key = "true" if key else "false"
            elif isinstance(key, int) and key < (1 << 63):
                key = str(key)
            elif isinstance(key, float):
                key = self._float_key(key)
            elif not isinstance(key, str):
                raise ConstructorError(None, None, "unsupported Kubernetes JSON object key", key_node.start_mark)
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping

    def construct_object(self, node: yaml.Node, deep: bool = False) -> Any:
        """Keep non-core scalar tags as text while SafeLoader owns collection construction."""
        if isinstance(node, yaml.ScalarNode) and node.tag not in self._scalar_tags and node.tag != "tag:yaml.org,2002:str":
            return self.construct_scalar(node)
        return super().construct_object(node, deep=deep)


for _tag in _ResourceLoader._scalar_tags:
    _ResourceLoader.add_constructor(_tag, _ResourceLoader._construct_scalar)


def decode_resources(text: str) -> Iterator[dict[str, Any]]:
    """Read a rendered Kubernetes resource stream using Go-compatible scalar interpretation."""
    yield from (value for value in yaml.load_all(text, Loader=_ResourceLoader) if value is not None)


def encode_resource(resource: dict[str, Any]) -> str:
    """Encode one YAML-derived resource with quoted strings and explicit non-string tags for kubectl."""
    return yaml.safe_dump(resource, sort_keys=False, default_style='"')


def encode_resources(resources: Iterable[dict[str, Any]]) -> str:
    """Encode a resource stream for Kustomize and kubectl without implicit scalar reinterpretation."""
    return yaml.safe_dump_all(resources, sort_keys=False, default_style='"')
