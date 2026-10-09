# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Bounded anonymous source selection for builds and platform OCI artifacts."""

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
from functools import cache, partial

_PROBE_TIMEOUT_SECONDS = 4.0
_PROBE_READ_BYTES = 64 * 1024
_MIRROR_FASTER_RATIO = 0.7
_MIRROR_FASTER_MARGIN_SECONDS = 0.25


_GITHUB_MIRRORS = (
    "https://gh-proxy.com/https://github.com",
    "https://ghproxy.net/https://github.com",
)


@dataclass(frozen=True)
class _SourceMirror:
    measure: Callable[[], float | None]
    value: str


@dataclass(frozen=True)
class _SourceProbe:
    name: str
    environment_name: str
    official: Callable[[], float | None]
    mirrors: tuple[_SourceMirror, ...]
    registry: str | None = None


@dataclass(frozen=True)
class BuildSourceSelection:
    """Automatic build settings; OCI mirrors retain their origins for native failover."""

    environment: dict[str, str]
    registry_mirrors: dict[str, list[str]]
    messages: tuple[str, ...]


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
        while len(body) < _PROBE_READ_BYTES:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise TimeoutError
            if connection.sock is not None:
                connection.sock.settimeout(timeout)
            chunk = response.read(
                min(
                    8192,
                    _PROBE_READ_BYTES - len(body),
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
        _read_url(url, started + _PROBE_TIMEOUT_SECONDS)
    except (OSError, TimeoutError, http.client.HTTPException, ssl.SSLError, ValueError):
        return None
    return time.monotonic() - started


def _measure_github(base: str) -> float | None:
    """Probe archive and Git access because build consumers use both transports."""
    started = time.monotonic()
    deadline = started + _PROBE_TIMEOUT_SECONDS
    repository = f"{base}/shiweijiezero/foretoken"
    try:
        refs = _read_url(f"{repository}.git/info/refs?service=git-upload-pack", deadline)
        if b"# service=git-upload-pack" not in refs:
            return None
        archive = _read_url(f"{repository}/archive/refs/heads/main.tar.gz", deadline)
        if not archive.startswith(b"\x1f\x8b"):
            return None
    except (OSError, TimeoutError, http.client.HTTPException, ssl.SSLError, ValueError):
        return None
    return time.monotonic() - started


def _measure_cargo(index: str) -> float | None:
    """Probe a sparse index and its advertised crate download under one deadline."""
    started = time.monotonic()
    deadline = started + _PROBE_TIMEOUT_SECONDS
    try:
        configuration = json.loads(_read_url(f"{index}/config.json", deadline))
        download = configuration.get("dl")
        if not isinstance(download, str):
            return None
        entry = _read_url(f"{index}/it/oa/itoa", deadline)
        if not any(json.loads(line).get("vers") == "1.0.18" for line in entry.splitlines()):
            return None
        if "{" in download:
            download = download.format(
                crate="itoa", version="1.0.18", prefix="it/oa", lowerprefix="it/oa"
            )
        else:
            download = f"{download.rstrip('/')}/itoa/1.0.18/download"
        if not _read_url(download, deadline).startswith(b"\x1f\x8b"):
            return None
    except (OSError, TimeoutError, http.client.HTTPException, ssl.SSLError, ValueError, KeyError):
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
    deadline = started + _PROBE_TIMEOUT_SECONDS
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
        for _ in range(4):
            status, response_headers, _ = _https_get(url, deadline, headers)
            if status == 200:
                return time.monotonic() - started
            if status in {301, 302, 303, 307, 308}:
                location = response_headers.get("location")
                if not location:
                    return None
                target = urllib.parse.urljoin(url, location)
                if urllib.parse.urlsplit(target).netloc != urllib.parse.urlsplit(url).netloc:
                    headers.pop("Authorization", None)
                url = target
                continue
            if status != 401 or "Authorization" in headers:
                return None
            parameters = _bearer_parameters(
                response_headers.get("www-authenticate", "")
            )
            realm = parameters.pop("realm", "")
            if not realm:
                return None
            token_url = f"{realm}?{urllib.parse.urlencode(parameters)}"
            token_status, _, token_body = _https_get(token_url, deadline)
            if token_status != 200:
                return None
            token = json.loads(token_body)
            access_token = token.get("token") or token.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                return None
            headers["Authorization"] = f"Bearer {access_token}"
    except (OSError, TimeoutError, http.client.HTTPException, ssl.SSLError, ValueError):
        return None
    return None


def _select_mirror(
    official: float | None,
    mirrors: tuple[_SourceMirror, ...],
    measurements: tuple[float | None, ...],
) -> tuple[_SourceMirror, float] | None:
    """Return the fastest usable mirror when it materially beats the official source."""
    available = tuple(
        (mirror, elapsed)
        for mirror, elapsed in zip(mirrors, measurements, strict=True)
        if elapsed is not None
    )
    if not available:
        return None
    mirror, elapsed = min(available, key=lambda item: item[1])
    if official is not None and not (
        elapsed <= official * _MIRROR_FASTER_RATIO
        and official - elapsed >= _MIRROR_FASTER_MARGIN_SECONDS
    ):
        return None
    return mirror, elapsed


def _oci_mirrors(host: str, repository: str, revision: str) -> tuple[_SourceMirror, ...]:
    """Build shared registry candidates for platform pulls and source-image builds."""
    prefixes = (f"m.daocloud.io/{host}",)
    if host == "docker.io":
        prefixes += ("docker.1ms.run",)
    candidates = []
    for prefix in prefixes:
        mirror_host, _, namespace = prefix.partition("/")
        mirror_repository = f"{namespace}/{repository}" if namespace else repository
        candidates.append(
            _SourceMirror(
                partial(_measure_oci_manifest, mirror_host, mirror_repository, revision),
                prefix,
            )
        )
    return tuple(candidates)


def platform_image_reference(reference: str, registry: str | None = None) -> str:
    """Resolve a platform-owned image default, preserving its tag or digest.

    An explicit registry replaces the original host. Automatic public proxies
    use their upstream-specific repository paths. Callers omit user-selected images.
    """
    first, separator, path = reference.partition("/")
    if registry is not None:
        qualified = separator and ("." in first or ":" in first or first == "localhost")
        repository = path if qualified else reference
        return f"{registry}/{repository}"
    return select_platform_oci_reference(reference)


@cache
def select_platform_oci_reference(reference: str) -> str:
    """Select an anonymous source for a CLI-owned public image or OCI chart default.

    Callers retain explicit user overrides. Preserve the original registry path,
    tag or digest when selecting the public proxy, and reuse the decision within
    this CLI invocation.
    """
    scheme = "oci://" if reference.startswith("oci://") else ""
    value = reference.removeprefix(scheme) if scheme else reference
    host, separator, path = value.partition("/")
    if not separator or host not in {
        "docker.io", "ghcr.io", "gcr.io", "registry.k8s.io", "quay.io", "nvcr.io",
        "cr.infini-ai.com",
    }:
        return reference
    if "@" in path:
        repository, revision = path.rsplit("@", 1)
    elif ":" in path.rsplit("/", 1)[-1]:
        repository, revision = path.rsplit(":", 1)
    else:
        repository, revision = path, "latest"
    official_host = "registry-1.docker.io" if host == "docker.io" else host
    mirrors = _oci_mirrors(host, repository, revision)
    with ThreadPoolExecutor(max_workers=1 + len(mirrors)) as executor:
        official = executor.submit(
            _measure_oci_manifest, official_host, repository, revision
        )
        futures = tuple(executor.submit(mirror.measure) for mirror in mirrors)
        selected = _select_mirror(
            official.result(), mirrors, tuple(future.result() for future in futures)
        )
    return f"{scheme}{selected[0].value}/{path}" if selected else reference


@cache
def select_github_download(url: str, mirror: str | None = None) -> str:
    """Select a source for a platform-owned GitHub download, preserving explicit overrides."""
    prefix = "https://github.com"
    if not url.startswith(prefix + "/"):
        return url
    path = url.removeprefix(prefix)
    if mirror:
        return mirror.rstrip("/") + path
    mirrors = tuple(
        _SourceMirror(partial(_measure_url, base + path), base + path)
        for base in _GITHUB_MIRRORS
    )
    with ThreadPoolExecutor(max_workers=1 + len(mirrors)) as executor:
        official = executor.submit(_measure_url, url)
        futures = tuple(executor.submit(candidate.measure) for candidate in mirrors)
        selected = _select_mirror(
            official.result(), mirrors, tuple(future.result() for future in futures)
        )
    return selected[0].value if selected else url


def select_build_sources(environment: Mapping[str, str]) -> BuildSourceSelection:
    """Select unconfigured build sources without turning OCI mirrors into fixed origins."""
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
                    _oci_mirrors("docker.io", "library/rust", "1.97.1-slim-bookworm"),
                    registry="docker.io",
                ),
                _SourceProbe(
                    "GCR",
                    "FORETOKEN_GCR_REGISTRY",
                    lambda: _measure_oci_manifest(
                        "gcr.io", "distroless/static-debian13", "nonroot"
                    ),
                    _oci_mirrors("gcr.io", "distroless/static-debian13", "nonroot"),
                    registry="gcr.io",
                ),
                _SourceProbe(
                    "GHCR",
                    "FORETOKEN_GHCR_REGISTRY",
                    lambda: _measure_oci_manifest(
                        "ghcr.io",
                        "shiweijiezero/foretoken/model-server",
                        "latest",
                    ),
                    _oci_mirrors(
                        "ghcr.io", "shiweijiezero/foretoken/model-server", "latest"
                    ),
                    registry="ghcr.io",
                ),
            )
        )
    probes.extend(
        (
            _SourceProbe(
                "PyPI",
                "UV_DEFAULT_INDEX",
                lambda: _measure_url("https://pypi.org/simple/modelscope/"),
                tuple(
                    _SourceMirror(partial(_measure_url, f"{index}/modelscope/"), index)
                    for index in (
                        "https://pypi.tuna.tsinghua.edu.cn/simple",
                        "https://mirrors.ustc.edu.cn/pypi/simple",
                        "https://mirrors.aliyun.com/pypi/simple",
                    )
                ),
            ),
            _SourceProbe(
                "Go module proxy",
                "GOPROXY",
                lambda: _measure_url(
                    "https://proxy.golang.org/golang.org/x/sync/@v/v0.20.0.info"
                ),
                tuple(
                    _SourceMirror(
                        partial(_measure_url, f"{proxy}/golang.org/x/sync/@v/v0.20.0.info"),
                        proxy,
                    )
                    for proxy in (
                        "https://goproxy.cn",
                        "https://mirrors.aliyun.com/goproxy",
                        "https://goproxy.io",
                    )
                ),
            ),
            _SourceProbe(
                "Cargo registry",
                "FORETOKEN_CARGO_REGISTRY",
                partial(_measure_cargo, "https://index.crates.io"),
                tuple(
                    _SourceMirror(partial(_measure_cargo, index), f"sparse+{index}/")
                    for index in (
                        "https://rsproxy.cn/index",
                        "https://mirrors.sjtug.sjtu.edu.cn/crates.io-index",
                    )
                ),
            ),
            _SourceProbe(
                "GitHub",
                "FORETOKEN_GITHUB_MIRROR",
                partial(_measure_github, "https://github.com"),
                tuple(
                    _SourceMirror(partial(_measure_github, base), base)
                    for base in _GITHUB_MIRRORS
                ),
            ),
        )
    )
    probes = [probe for probe in probes if not environment.get(probe.environment_name)]
    if not probes:
        return BuildSourceSelection({}, {}, ())

    with ThreadPoolExecutor(
        max_workers=sum(1 + len(probe.mirrors) for probe in probes)
    ) as executor:
        measurements = tuple(
            (
                probe,
                executor.submit(probe.official),
                tuple(
                    executor.submit(mirror.measure) for mirror in probe.mirrors
                ),
            )
            for probe in probes
        )
        results = tuple(
            (
                probe,
                official.result(),
                tuple(mirror.result() for mirror in mirrors),
            )
            for probe, official, mirrors in measurements
        )

    selected: dict[str, str] = {}
    registry_mirrors: dict[str, list[str]] = {}
    messages: list[str] = []
    for probe, official, mirror_measurements in results:
        selected_mirror = _select_mirror(
            official, probe.mirrors, mirror_measurements
        )
        if selected_mirror is None:
            continue
        mirror, mirror_time = selected_mirror
        if probe.registry is not None:
            registry_mirrors[probe.registry] = [mirror.value]
        else:
            selected[probe.environment_name] = mirror.value
        official_time = "unavailable" if official is None else f"{official:.2f}s"
        messages.append(
            f"{probe.name}: {mirror.value} {mirror_time:.2f}s, official {official_time}"
        )

    return BuildSourceSelection(selected, registry_mirrors, tuple(messages))


@cache
def select_huggingface_endpoint(
    repository: str, explicit_endpoint: str | None = None
) -> str | None:
    """Select an explicit or reachable Hugging Face endpoint for one repository."""
    if explicit_endpoint:
        return explicit_endpoint.rstrip("/")

    encoded = urllib.parse.quote(repository, safe="/")
    official_endpoint = "https://huggingface.co"
    mirrors = tuple(
        _SourceMirror(partial(_measure_url, f"{endpoint}/api/models/{encoded}"), endpoint)
        for endpoint in ("https://hf-mirror.com",)
    )
    with ThreadPoolExecutor(max_workers=1 + len(mirrors)) as executor:
        official = executor.submit(_measure_url, f"{official_endpoint}/api/models/{encoded}")
        futures = tuple(executor.submit(mirror.measure) for mirror in mirrors)
        official_time = official.result()
        selected = _select_mirror(
            official_time, mirrors, tuple(future.result() for future in futures)
        )
    if selected:
        return selected[0].value
    return official_endpoint if official_time is not None else None
