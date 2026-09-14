# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Bounded anonymous source selection for source-image builds."""

from __future__ import annotations

import http.client
import json
import re
import ssl
import time
import urllib.parse
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass


@dataclass(frozen=True)
class _SourceSelectionPolicy:
    """Shared internal thresholds for anonymous source selection."""

    probe_timeout_seconds: float
    probe_read_bytes: int
    faster_ratio: float
    faster_margin_seconds: float


SOURCE_SELECTION_POLICY = _SourceSelectionPolicy(
    probe_timeout_seconds=4.0,
    probe_read_bytes=64 * 1024,
    faster_ratio=0.7,
    faster_margin_seconds=0.25,
)


@dataclass(frozen=True)
class _SourceProbe:
    name: str
    environment_name: str
    official: Callable[[], float | None]
    mirror: Callable[[], float | None] | None
    environment_value: str | None = None


def _https_get(
    url: str,
    deadline: float,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Read one direct HTTPS resource under a single connection and transfer deadline."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"source probe requires HTTPS: {url}")
    timeout = deadline - time.monotonic()
    if timeout <= 0:
        raise TimeoutError
    connection = http.client.HTTPSConnection(
        parsed.hostname,
        parsed.port,
        timeout=timeout,
        context=ssl.create_default_context(),
    )
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        body = bytearray()
        while len(body) < SOURCE_SELECTION_POLICY.probe_read_bytes:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise TimeoutError
            if connection.sock is not None:
                connection.sock.settimeout(timeout)
            chunk = response.read(
                min(
                    8192,
                    SOURCE_SELECTION_POLICY.probe_read_bytes - len(body),
                )
            )
            if not chunk:
                break
            body.extend(chunk)
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        return response.status, response_headers, bytes(body)
    finally:
        connection.close()


def _read_url(url: str, deadline: float) -> bytes:
    """Follow a short redirect chain while preserving one end-to-end deadline."""
    for _ in range(4):
        status, headers, body = _https_get(
            url,
            deadline,
            {"User-Agent": "foretoken-source-probe"},
        )
        if status not in {301, 302, 303, 307, 308}:
            if status != 200:
                raise OSError(f"source probe returned HTTP {status}")
            return body
        location = headers.get("location")
        if not location:
            raise OSError("source probe redirect has no location")
        url = urllib.parse.urljoin(url, location)
    raise OSError("source probe returned too many redirects")


def _measure_url(url: str) -> float | None:
    """Measure a bounded anonymous GET of one package or source resource."""
    started = time.monotonic()
    try:
        _read_url(url, started + SOURCE_SELECTION_POLICY.probe_timeout_seconds)
    except (OSError, TimeoutError, http.client.HTTPException, ssl.SSLError, ValueError):
        return None
    return time.monotonic() - started


def _bearer_parameters(header: str) -> dict[str, str]:
    """Decode the standard registry Bearer challenge fields used for anonymous pulls."""
    if not header.startswith("Bearer "):
        return {}
    return dict(re.findall(r'(\w+)="([^"]+)"', header[7:]))


def _measure_oci_manifest(host: str, repository: str, reference: str) -> float | None:
    """Measure an anonymous OCI manifest fetch, including its registry token exchange."""
    started = time.monotonic()
    deadline = started + SOURCE_SELECTION_POLICY.probe_timeout_seconds
    url = f"https://{host}/v2/{repository}/manifests/{reference}"
    headers = {
        "Accept": (
            "application/vnd.oci.image.index.v1+json,"
            "application/vnd.oci.image.manifest.v1+json,"
            "application/vnd.docker.distribution.manifest.list.v2+json,"
            "application/vnd.docker.distribution.manifest.v2+json"
        ),
        "User-Agent": "foretoken-source-probe",
    }
    try:
        status, response_headers, _ = _https_get(url, deadline, headers)
        if status == 401:
            parameters = _bearer_parameters(
                response_headers.get("www-authenticate", "")
            )
            realm = parameters.pop("realm", "")
            if not realm:
                return None
            token_url = f"{realm}?{urllib.parse.urlencode(parameters)}"
            _, _, token_body = _https_get(token_url, deadline)
            token = json.loads(token_body)
            access_token = token.get("token") or token.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                return None
            headers["Authorization"] = f"Bearer {access_token}"
            status, _, _ = _https_get(url, deadline, headers)
        if status != 200:
            return None
    except (OSError, TimeoutError, http.client.HTTPException, ssl.SSLError, ValueError):
        return None
    return time.monotonic() - started


def _prefer_mirror(official: float | None, mirror: float | None) -> bool:
    """Apply the shared materially-faster threshold to one source pair."""
    if mirror is None:
        return False
    if official is None:
        return True
    return (
        mirror <= official * SOURCE_SELECTION_POLICY.faster_ratio
        and official - mirror >= SOURCE_SELECTION_POLICY.faster_margin_seconds
    )


def select_source_build_sources(
    environment: Mapping[str, str],
) -> tuple[dict[str, str], tuple[str, ...], tuple[str, ...]]:
    """Measure unconfigured official and anonymous sources and return faster build settings."""
    probes: list[_SourceProbe] = []
    if not environment.get("FORETOKEN_OCI_REGISTRY"):
        probes.extend(
            (
                _SourceProbe(
                    "Docker Hub",
                    "FORETOKEN_DOCKER_IO_REGISTRY",
                    lambda: _measure_oci_manifest(
                        "registry-1.docker.io",
                        "library/rust",
                        "1.97.1-slim-bookworm",
                    ),
                    lambda: _measure_oci_manifest(
                        "m.daocloud.io",
                        "docker.io/library/rust",
                        "1.97.1-slim-bookworm",
                    ),
                    "m.daocloud.io/docker.io",
                ),
                _SourceProbe(
                    "GCR",
                    "FORETOKEN_GCR_REGISTRY",
                    lambda: _measure_oci_manifest(
                        "gcr.io", "distroless/static-debian13", "nonroot"
                    ),
                    lambda: _measure_oci_manifest(
                        "m.daocloud.io",
                        "gcr.io/distroless/static-debian13",
                        "nonroot",
                    ),
                    "m.daocloud.io/gcr.io",
                ),
                _SourceProbe(
                    "GHCR",
                    "FORETOKEN_GHCR_REGISTRY",
                    lambda: _measure_oci_manifest(
                        "ghcr.io",
                        "shiweijiezero/foretoken/model-server",
                        "latest",
                    ),
                    lambda: _measure_oci_manifest(
                        "m.daocloud.io",
                        "ghcr.io/shiweijiezero/foretoken/model-server",
                        "latest",
                    ),
                    "m.daocloud.io/ghcr.io",
                ),
            )
        )
    probes.extend(
        (
            _SourceProbe(
                "PyPI",
                "UV_DEFAULT_INDEX",
                lambda: _measure_url("https://pypi.org/simple/modelscope/"),
                lambda: _measure_url(
                    "https://pypi.tuna.tsinghua.edu.cn/simple/modelscope/"
                ),
                "https://pypi.tuna.tsinghua.edu.cn/simple",
            ),
            _SourceProbe(
                "Go module proxy",
                "GOPROXY",
                lambda: _measure_url(
                    "https://proxy.golang.org/golang.org/x/sync/@v/v0.20.0.info"
                ),
                lambda: _measure_url(
                    "https://goproxy.cn/golang.org/x/sync/@v/v0.20.0.info"
                ),
                "https://goproxy.cn",
            ),
            _SourceProbe(
                "Cargo registry",
                "FORETOKEN_CARGO_REGISTRY",
                lambda: _measure_url(
                    "https://static.crates.io/crates/itoa/itoa-1.0.18.crate"
                ),
                lambda: _measure_url(
                    "https://rsproxy.cn/api/v1/crates/itoa/1.0.18/download"
                ),
                "sparse+https://rsproxy.cn/index/",
            ),
            _SourceProbe(
                "GitHub",
                "FORETOKEN_GITHUB_MIRROR",
                lambda: _measure_url(
                    "https://github.com/shiweijiezero/foretoken/"
                    "archive/refs/heads/main.tar.gz"
                ),
                lambda: _measure_url(
                    "https://ghproxy.net/https://github.com/shiweijiezero/"
                    "foretoken/archive/refs/heads/main.tar.gz"
                ),
                "https://ghproxy.net/https://github.com",
            ),
        )
    )
    probes = [probe for probe in probes if not environment.get(probe.environment_name)]
    if not probes:
        return {}, (), ()

    with ThreadPoolExecutor(max_workers=len(probes) * 2) as executor:
        measurements = tuple(
            (
                probe,
                executor.submit(probe.official),
                executor.submit(probe.mirror) if probe.mirror is not None else None,
            )
            for probe in probes
        )
        results = tuple(
            (
                probe,
                official.result(),
                mirror.result() if mirror is not None else None,
            )
            for probe, official, mirror in measurements
        )

    selected: dict[str, str] = {}
    messages: list[str] = []
    unavailable: list[str] = []
    for probe, official, mirror in results:
        if official is None and mirror is None:
            unavailable.append(probe.name)
            continue
        if not _prefer_mirror(official, mirror):
            continue
        assert mirror is not None
        assert probe.environment_value is not None
        selected[probe.environment_name] = probe.environment_value
        official_time = "unavailable" if official is None else f"{official:.2f}s"
        messages.append(f"{probe.name}: mirror {mirror:.2f}s, official {official_time}")

    return selected, tuple(messages), tuple(unavailable)
