# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Bounded anonymous source selection for builds and platform OCI artifacts."""

from __future__ import annotations

import json
import ssl
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Callable, Collection, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from functools import cache, partial
from pathlib import Path
from urllib.request import parse_http_list, parse_keqv_list

import httpx

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


def platform_image_reference(
    reference: str, registry: str | None = None, *, previous: str | None = None
) -> str:
    """Resolve a platform image default, retaining an available prior source for the same image.

    An explicit registry replaces the original host. Automatic public proxies
    use their upstream-specific repository paths. Callers omit user-selected images.
    """
    first, separator, path = reference.partition("/")
    if registry is not None:
        qualified = separator and ("." in first or ":" in first or first == "localhost")
        repository = path if qualified else reference
        return f"{registry}/{repository}"
    return select_platform_oci_reference(reference, previous)


def select_platform_image_sources(
    references: Mapping[str, str],
    registry: str | None = None,
    *,
    previous: Mapping[str, str] | None = None,
    installed_images: Collection[str] = (),
) -> dict[str, str]:
    """Resolve image defaults concurrently, retaining matching installed sources while reachable."""
    previous = previous or {}
    with ThreadPoolExecutor() as executor:
        # Coalesce shared images before submission; the per-image cache does not
        # prevent duplicate probes when concurrent callers both miss it.
        pending = {}
        futures = {}
        for key, reference in references.items():
            prior = previous.get(key)
            if prior is None and installed_images:
                host, _, path = reference.partition("/")
                candidates = (
                    reference,
                    *(f"{prefix}/{path}" for prefix in _oci_mirror_prefixes(host)),
                )
                prior = next(
                    (
                        candidate
                        for candidate in candidates
                        if candidate in installed_images
                    ),
                    None,
                )
            selection = (reference, prior)
            if selection not in pending:
                pending[selection] = executor.submit(
                    copy_context().run, platform_image_reference, reference, registry, previous=prior
                )
            futures[key] = pending[selection]
        return {key: future.result() for key, future in futures.items()}


@cache
def select_platform_oci_reference(reference: str, previous: str | None = None) -> str:
    """Select an anonymous source for a CLI-owned public image or OCI chart default.

    Callers retain explicit user overrides. Preserve the original registry path,
    tag or digest when selecting the public proxy, and reuse the decision within
    this CLI invocation. A matching prior reference remains selected while reachable.
    """
    scheme = "oci://" if reference.startswith("oci://") else ""
    value = reference.removeprefix(scheme) if scheme else reference
    host, separator, path = value.partition("/")
    if not separator or not _oci_mirror_prefixes(host):
        return reference
    if "@" in path:
        repository, revision = path.rsplit("@", 1)
    elif ":" in path.rsplit("/", 1)[-1]:
        repository, revision = path.rsplit(":", 1)
    else:
        repository, revision = path, "latest"
    official_host = "registry-1.docker.io" if host == "docker.io" else host
    mirrors = _oci_mirrors(host, repository, revision)
    preferred = (
        ""
        if previous == reference
        else next(
            (
                mirror.value
                for mirror in mirrors
                if previous == f"{scheme}{mirror.value}/{path}"
            ),
            None,
        )
    )
    with ThreadPoolExecutor(max_workers=1 + len(mirrors)) as executor:
        official = executor.submit(
            copy_context().run, _measure, "oci", official_host, repository, revision
        )
        futures = tuple(executor.submit(copy_context().run, mirror.measure) for mirror in mirrors)
        selected = _select_mirror(
            official.result(),
            mirrors,
            tuple(future.result() for future in futures),
            preferred=preferred,
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
        _SourceMirror(partial(_measure, "url", base + path), base + path)
        for base in _GITHUB_MIRRORS
    )
    with ThreadPoolExecutor(max_workers=1 + len(mirrors)) as executor:
        official = executor.submit(copy_context().run, _measure, "url", url)
        futures = tuple(executor.submit(copy_context().run, candidate.measure) for candidate in mirrors)
        selected = _select_mirror(
            official.result(), mirrors, tuple(future.result() for future in futures)
        )
    return selected[0].value if selected else url


def select_build_sources(
    environment: Mapping[str, str],
    *,
    previous_environment: Mapping[str, str] | None = None,
    previous_registry_mirrors: Mapping[str, list[str]] | None = None,
) -> BuildSourceSelection:
    """Select unconfigured build sources, retaining reachable choices from the previous build."""
    probes: list[_SourceProbe] = []
    if not environment.get("FORETOKEN_OCI_REGISTRY"):
        probes.extend(
            (
                _SourceProbe(
                    "Docker Hub",
                    "FORETOKEN_DOCKER_IO_REGISTRY",
                    lambda: _measure(
                        "oci",
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
                    lambda: _measure(
                        "oci",
                        "gcr.io", "distroless/static-debian13", "nonroot"
                    ),
                    _oci_mirrors("gcr.io", "distroless/static-debian13", "nonroot"),
                    registry="gcr.io",
                ),
                _SourceProbe(
                    "GHCR",
                    "FORETOKEN_GHCR_REGISTRY",
                    lambda: _measure(
                        "oci",
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
                partial(_measure, "url", "https://pypi.org/simple/modelscope/"),
                tuple(
                    _SourceMirror(partial(_measure, "url", f"{index}/modelscope/"), index)
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
                partial(
                    _measure, "url",
                    "https://proxy.golang.org/golang.org/x/sync/@v/v0.20.0.info",
                ),
                tuple(
                    _SourceMirror(
                        partial(_measure, "url", f"{proxy}/golang.org/x/sync/@v/v0.20.0.info"),
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
                partial(_measure, "cargo", "https://index.crates.io"),
                tuple(
                    _SourceMirror(partial(_measure, "cargo", index), f"sparse+{index}/")
                    for index in (
                        "https://rsproxy.cn/index",
                        "https://mirrors.sjtug.sjtu.edu.cn/crates.io-index",
                    )
                ),
            ),
            _SourceProbe(
                "GitHub",
                "FORETOKEN_GITHUB_MIRROR",
                partial(_measure, "github", "https://github.com"),
                tuple(
                    _SourceMirror(partial(_measure, "github", base), base)
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
                executor.submit(copy_context().run, probe.official),
                tuple(
                    executor.submit(copy_context().run, mirror.measure) for mirror in probe.mirrors
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
        preferred = None
        if probe.registry is not None and previous_registry_mirrors is not None:
            prior = previous_registry_mirrors.get(probe.registry, [])
            preferred = prior[0] if prior else ""
        elif probe.registry is None and previous_environment is not None:
            preferred = previous_environment.get(probe.environment_name, "")
        selected_mirror = _select_mirror(
            official, probe.mirrors, mirror_measurements, preferred=preferred
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
        _SourceMirror(partial(_measure, "url", f"{endpoint}/api/models/{encoded}"), endpoint)
        for endpoint in ("https://hf-mirror.com",)
    )
    with ThreadPoolExecutor(max_workers=1 + len(mirrors)) as executor:
        official = executor.submit(copy_context().run, _measure, "url", f"{official_endpoint}/api/models/{encoded}")
        futures = tuple(executor.submit(copy_context().run, mirror.measure) for mirror in mirrors)
        official_time = official.result()
        selected = _select_mirror(
            official_time, mirrors, tuple(future.result() for future in futures)
        )
    if selected:
        return selected[0].value
    return official_endpoint if official_time is not None else None


def _measure(probe: str, *arguments: str) -> float | None:
    """Own the complete resolver and HTTP lifetime through the operation's command executor."""
    # The same file runs standalone in the probe process; only its parent needs
    # the CLI lifecycle. A stuck system resolver must terminate with that process.
    # Safe-path mode retains user-site installs without importing from this source directory.
    from foretoken.errors import DeploymentError
    from foretoken.processes.execution import CommandExecution, run_command

    execution = CommandExecution.current()
    try:
        completed = run_command(
            [sys.executable, "-P", "-B", str(Path(__file__).resolve())],
            input=json.dumps({"probe": probe, "arguments": arguments}),
            text=True,
            capture_output=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except DeploymentError as error:
        if execution is not None:
            execution.check()
        cause = error.__cause__
        # A stricter inherited operation deadline must remain an operation failure.
        if isinstance(cause, subprocess.TimeoutExpired) and cause.timeout == _PROBE_TIMEOUT_SECONDS:
            return None
        raise
    if execution is not None:
        execution.check()
    if completed.returncode:
        raise DeploymentError(
            "source probe failed: " + (completed.stderr.strip() or f"exit status {completed.returncode}")
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise DeploymentError("source probe returned invalid JSON") from error
    if value is not None and not isinstance(value, float):
        raise DeploymentError("source probe returned an unexpected result")
    return value


def _require_https(request: httpx.Request) -> None:
    """Keep anonymous probes and registry token redirects on credential-free HTTPS URLs."""
    if request.url.scheme != "https" or request.url.username or request.url.password:
        raise ValueError("source probes require HTTPS URLs without embedded credentials")


def _https_get(
    url: str,
    headers: dict[str, str] | None = None,
) -> tuple[httpx.Response, bytes]:
    """Read a bounded sample inside the process whose parent owns the complete probe deadline."""
    with httpx.Client(
        timeout=None,
        verify=ssl.create_default_context(),
        event_hooks={"request": [_require_https]},
        headers={"User-Agent": "foretoken-source-probe", "Accept-Encoding": "identity"},
    ) as client:
        request = client.build_request("GET", url, headers=headers)
        for _ in range(4):
            response = client.send(request, stream=True)
            try:
                # HTTPX owns redirect construction and credential stripping.
                # Skip redirect bodies rather than buffering an unbounded response.
                if response.next_request is not None:
                    request = response.next_request
                    continue
                body = bytearray()
                for chunk in response.iter_raw():
                    body.extend(chunk[:_PROBE_READ_BYTES - len(body)])
                    if len(body) == _PROBE_READ_BYTES:
                        break
                return response, bytes(body)
            finally:
                response.close()
        raise httpx.TooManyRedirects("source probe returned too many redirects", request=request)


def _read_url(url: str) -> bytes:
    """Read one anonymous source inside the owning probe process."""
    response, body = _https_get(url)
    if response.status_code != 200:
        raise OSError(f"source probe returned HTTP {response.status_code}")
    return body


def _probe_url(url: str) -> float | None:
    """Measure a bounded anonymous GET of one package or source resource."""
    started = time.monotonic()
    try:
        _read_url(url)
    except (OSError, httpx.HTTPError, httpx.InvalidURL, ValueError):
        return None
    return time.monotonic() - started


def _probe_github(base: str) -> float | None:
    """Probe archive and Git access because build consumers use both transports."""
    started = time.monotonic()
    repository = f"{base}/shiweijiezero/foretoken"
    try:
        refs = _read_url(f"{repository}.git/info/refs?service=git-upload-pack")
        if b"# service=git-upload-pack" not in refs:
            return None
        archive = _read_url(f"{repository}/archive/refs/heads/main.tar.gz")
        if not archive.startswith(b"\x1f\x8b"):
            return None
    except (OSError, httpx.HTTPError, httpx.InvalidURL, ValueError):
        return None
    return time.monotonic() - started


def _probe_cargo(index: str) -> float | None:
    """Probe a sparse index and its advertised crate download under one deadline."""
    started = time.monotonic()
    try:
        configuration = json.loads(_read_url(f"{index}/config.json"))
        download = configuration.get("dl")
        if not isinstance(download, str):
            return None
        entry = _read_url(f"{index}/it/oa/itoa")
        if not any(json.loads(line).get("vers") == "1.0.18" for line in entry.splitlines()):
            return None
        if "{" in download:
            download = download.format(
                crate="itoa", version="1.0.18", prefix="it/oa", lowerprefix="it/oa"
            )
        else:
            download = f"{download.rstrip('/')}/itoa/1.0.18/download"
        if not _read_url(download).startswith(b"\x1f\x8b"):
            return None
    except (OSError, httpx.HTTPError, httpx.InvalidURL, ValueError, KeyError):
        return None
    return time.monotonic() - started


def _bearer_parameters(header: str) -> dict[str, str]:
    """Decode the standard registry Bearer challenge fields used for anonymous pulls."""
    scheme, _, parameters = header.partition(" ")
    return parse_keqv_list(parse_http_list(parameters)) if scheme.lower() == "bearer" else {}


def _probe_oci_manifest(host: str, repository: str, reference: str) -> float | None:
    """Measure an anonymous OCI manifest fetch, including its registry token exchange."""
    started = time.monotonic()
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
            response, _ = _https_get(url, headers)
            if response.status_code == 200:
                return time.monotonic() - started
            if response.status_code != 401 or "Authorization" in response.request.headers:
                return None
            # HTTPX removes Authorization on cross-origin redirects. Continue at
            # that final origin so its token is never sent to the original host.
            url = str(response.url)
            parameters = _bearer_parameters(response.headers.get("www-authenticate", ""))
            realm = parameters.pop("realm", "")
            if not realm:
                return None
            token_url = str(httpx.URL(realm).copy_merge_params(parameters))
            token_response, token_body = _https_get(token_url)
            if token_response.status_code != 200:
                return None
            token = json.loads(token_body)
            access_token = token.get("token") or token.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                return None
            headers["Authorization"] = f"Bearer {access_token}"
    except (OSError, httpx.HTTPError, httpx.InvalidURL, ValueError):
        return None
    return None


def _select_mirror(
    official: float | None,
    mirrors: tuple[_SourceMirror, ...],
    measurements: tuple[float | None, ...],
    *,
    preferred: str | None = None,
) -> tuple[_SourceMirror, float] | None:
    """Retain an available prior source, otherwise select a materially faster mirror."""
    # An empty prior selection denotes the official source; None is a first selection.
    if preferred == "" and official is not None:
        return None
    available = tuple(
        (mirror, elapsed)
        for mirror, elapsed in zip(mirrors, measurements, strict=True)
        if elapsed is not None
    )
    if not available:
        return None
    for mirror, elapsed in available:
        if mirror.value == preferred:
            return mirror, elapsed
    mirror, elapsed = min(available, key=lambda item: item[1])
    if official is not None and not (
        elapsed <= official * _MIRROR_FASTER_RATIO
        and official - elapsed >= _MIRROR_FASTER_MARGIN_SECONDS
    ):
        return None
    return mirror, elapsed


def _oci_mirror_prefixes(host: str) -> tuple[str, ...]:
    """Define the public registry routes used by probes and installed-source matching."""
    if host not in {
        "docker.io",
        "ghcr.io",
        "gcr.io",
        "registry.k8s.io",
        "quay.io",
        "nvcr.io",
        "cr.infini-ai.com",
    }:
        return ()
    prefixes = (f"m.daocloud.io/{host}",)
    return prefixes + ("docker.1ms.run",) if host == "docker.io" else prefixes


def _oci_mirrors(
    host: str, repository: str, revision: str
) -> tuple[_SourceMirror, ...]:
    """Build shared registry candidates for platform pulls and source-image builds."""
    candidates = []
    for prefix in _oci_mirror_prefixes(host):
        mirror_host, _, namespace = prefix.partition("/")
        mirror_repository = f"{namespace}/{repository}" if namespace else repository
        candidates.append(
            _SourceMirror(
                partial(
                    _measure, "oci", mirror_host, mirror_repository, revision
                ),
                prefix,
            )
        )
    return tuple(candidates)


def _run_probe() -> None:
    """Execute the parent's private probe request and publish only network elapsed time or null."""
    request = json.load(sys.stdin)
    probes = {
        "url": _probe_url,
        "github": _probe_github,
        "cargo": _probe_cargo,
        "oci": _probe_oci_manifest,
    }
    print(json.dumps(probes[request["probe"]](*request["arguments"])), flush=True)


if __name__ == "__main__":
    _run_probe()
