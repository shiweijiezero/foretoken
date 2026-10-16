# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Resolve the model service a benchmark measures, including temporary Kustomize deployments."""

from __future__ import annotations

import asyncio
import logging
import shlex
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from foretoken.deployment import DeploymentLifecycle
from foretoken.kubernetes import (
    Kubectl,
    load_deployment,
    resolve_frontend_endpoint,
    timeout_seconds,
    wait_for_resources,
)
from foretoken.manifest import DeploymentError, ForetokenDeployment, ResourceRef

from benchmarks.config.benchmark import BenchmarkConfig, ModelServiceSource
from benchmarks.profiling import CaptureCleanupError

logger = logging.getLogger(__name__)


async def require_health_endpoint(url: str) -> None:
    """Check one public health URL without forwarding service credentials or routing headers."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("health URL must be an absolute HTTP or HTTPS URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("health URL contains an invalid port") from exc
    hostname = parsed.hostname
    if ":" in hostname:
        hostname = f"[{hostname}]"
    if port is not None:
        hostname = f"{hostname}:{port}"
    display_url = urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ValueError(
            f"Health check failed for {display_url}: HTTP {exc.response.status_code}"
        ) from exc
    except httpx.RequestError as exc:
        raise ValueError(
            f"Health check failed for {display_url}: {type(exc).__name__}"
        ) from exc


@dataclass(frozen=True)
class ModelService:
    """Describe one OpenAI-compatible model service that is ready for benchmark requests.

    ``chat_completions_url`` preserves the public endpoint selected by the user or
    deployment. ``routing_host`` is the HTTP ``Host`` header a Gateway HTTP listener
    needs to route by hostname; it is empty for LoadBalancer, HTTPS, and user-supplied
    URLs. ``model_service_refs`` contains the Kubernetes status sources that serve
    the selected model and is empty for a user-supplied URL. Every client derives
    its ``Authorization`` header from ``api_key``.
    """

    chat_completions_url: str
    model: str
    api_key: str
    models: tuple[str, ...]
    hostname: str
    gpu_count: int | None
    routing_host: str
    model_service_refs: tuple[ResourceRef, ...]
    # Capture must use the same rendered target that supplied the HTTP endpoint.
    deployment: ForetokenDeployment | None = None

    @property
    def api_root(self) -> str:
        """Return the OpenAI client base URL from either completion endpoint."""
        return self.chat_completions_url.rstrip("/").removesuffix(
            "/chat/completions"
        ).removesuffix("/completions")

    @property
    def tokenizer_identity(self) -> tuple[str, str]:
        """Return the selected source and client-side tokenizer, reusing deployment directories."""
        if not self.model:
            raise ValueError("Automatic tokenizer resolution requires one selected model; pass --model or a tokenizer override")
        if self.deployment is None:
            return "hf", self.model
        identities = {
            (spec.get("source", "hf"), spec.get("tokenizer") or spec["model"])
            for document in self.deployment.objects
            if document["kind"] == "ModelService"
            and (spec := document["spec"])["model"] == self.model
        }
        if len(identities) != 1:
            raise ValueError("The deployment must select one model/tokenizer identity")
        source, tokenizer = identities.pop()
        if source == "local" and not Path(tokenizer).expanduser().exists():
            from benchmarks.datasets.huggingface import find_local_tokenizer_path

            for cache in self.deployment.runtime_caches:
                if not cache.directory:
                    continue
                data_root = self.deployment.path / Path(cache.directory).expanduser()
                local = find_local_tokenizer_path(tokenizer, data_root=data_root)
                if local is not None:
                    return source, local
        return source, tokenizer

    def select_models(self, models: Iterable[str]) -> ModelService:
        """Align result and observation scope with the models selected by prepared requests."""
        selected = tuple(sorted(set(models)))
        model = selected[0] if len(selected) == 1 else ""
        if self.deployment is None:
            return replace(self, model=model, models=selected)
        unknown = set(selected) - set(self.deployment.models.values())
        if unknown:
            raise ValueError("Request models are not declared by the deployment: " + ", ".join(sorted(unknown)))
        refs = tuple(
            ResourceRef("ModelService", name, self.deployment.namespace)
            for name, value in sorted(self.deployment.models.items())
            if value in selected
        )
        gpu_count = self.gpu_count if model == self.model else (
            _model_gpu_count(self.deployment, model, Kubectl()) if model else None
        )
        return replace(self, model=model, models=selected, model_service_refs=refs, gpu_count=gpu_count)

    @property
    def request_headers(self) -> dict[str, str]:
        """Return the routing headers every request must carry, without credentials."""
        return {"Host": self.routing_host} if self.routing_host else {}


def _select_model(models: Iterable[str], requested: str) -> str:
    available = sorted(set(models))
    if requested:
        if requested not in available:
            raise DeploymentError(
                f"model {requested!r} is not declared by the deployment; "
                f"available models: {', '.join(available)}"
            )
        return requested
    if len(available) != 1:
        raise DeploymentError(
            "the deployment declares multiple models; pass --model with one of: "
            + ", ".join(available)
        )
    return available[0]


def _model_service_refs(
    deployment: ForetokenDeployment, model: str
) -> tuple[ResourceRef, ...]:
    """Return identities for the selected model, or the deployment's full mixed-model scope."""
    return tuple(
        ResourceRef("ModelService", name, deployment.namespace)
        for name, value in sorted(deployment.models.items())
        if not model or value == model
    )


def _model_gpu_count(
    deployment: ForetokenDeployment, model: str, kubectl: Kubectl,
) -> int:
    """Count allocated GPUs in the service-selected, ready execution groups."""
    services = kubectl.get_resources(_model_service_refs(deployment, model))
    selected = {
        (pool["poolUID"], pool["revision"])
        for service in services
        for pool in service["status"]["servingPoolRevisions"]
    }
    total = 0
    for group in kubectl.list_resources(("modelgroups",), deployment.namespace):
        spec = group["spec"]
        if (spec["modelPoolRef"]["uid"], spec["revision"]) not in selected:
            continue
        if group["metadata"].get("deletionTimestamp") or group["status"].get("phase") != "Ready":
            continue
        total += int(spec["nodeCount"]) * int(spec["resources"]["requests"]["gpu"]["count"])
    if total < 1:
        raise DeploymentError(f"no resolved GPU capacity for model {model!r}")
    return total


def _wait_for_models(
    api_root: str,
    headers: dict[str, str],
    timeout: float,
    expected_models: Iterable[str],
    api_key: str,
) -> tuple[str, ...]:
    expected = set(expected_models)
    deadline = time.monotonic() + timeout
    request_headers = {**headers, "Authorization": f"Bearer {api_key}"}
    last_error = "frontend has not responded"
    with httpx.Client(
        headers=request_headers,
        timeout=5.0,
        follow_redirects=True,
    ) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(f"{api_root}/models")
                response.raise_for_status()
                models = tuple(
                    sorted(
                        str(item["id"])
                        for item in response.json().get("data", [])
                        if isinstance(item, dict) and item.get("id")
                    )
                )
                if expected.issubset(models):
                    return models
                last_error = (
                    f"frontend advertises {models or '<no models>'}; "
                    f"waiting for {tuple(sorted(expected))}"
                )
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                last_error = str(exc)
            time.sleep(2)
    raise DeploymentError(f"frontend did not become ready: {last_error}")


def _discover_model_service(
    deployment: ForetokenDeployment,
    kubectl: Kubectl,
    source: ModelServiceSource,
    *,
    allow_multiple_models: bool = False,
) -> ModelService:
    """Wait for the rendered service to become ready and return its public model service."""
    wait_seconds = timeout_seconds(source.wait_timeout)
    model = (
        "" if allow_multiple_models and not source.model and len(set(deployment.models.values())) > 1
        else _select_model(deployment.models.values(), source.model)
    )
    model_refs = _model_service_refs(deployment, model)
    resources = (ResourceRef("FrontendService", deployment.frontend, deployment.namespace), *model_refs)
    progress = wait_for_resources(resources, kubectl, source.wait_timeout)
    for service in progress:
        if service.state == "Stopped":
            raise DeploymentError(
                f"{service.resource.display_name} is stopped; resume it before benchmarking with:\n"
                f"foretoken deploy {shlex.quote(str(deployment.path))}"
            )
    gpu_count = _model_gpu_count(deployment, model, kubectl) if model else None
    endpoint = resolve_frontend_endpoint(deployment, kubectl, source.wait_timeout)
    chat_completions_url = f"{endpoint.url}/v1/chat/completions"
    api_root = f"{endpoint.url}/v1"
    headers = {"Host": endpoint.routing_host} if endpoint.routing_host else {}
    models = _wait_for_models(
        api_root,
        headers,
        wait_seconds,
        (model,) if model else deployment.models.values(),
        source.api_key,
    )
    return ModelService(
        chat_completions_url=chat_completions_url,
        model=model,
        api_key=source.api_key,
        models=models,
        hostname=deployment.hostname,
        gpu_count=gpu_count,
        routing_host=endpoint.routing_host,
        model_service_refs=model_refs,
        deployment=deployment,
    )


def _service_presence(
    deployment: ForetokenDeployment,
    kubectl: Kubectl,
) -> list[bool]:
    return [
        kubectl.exists(resource.kind, resource.name, resource.namespace)
        for resource in deployment.service_refs()
    ]


@contextmanager
def resolve_benchmark_service(benchmark: BenchmarkConfig) -> Iterator[ModelService]:
    """Prepare a performance service for a point or a contiguous group of sweep points."""
    workload = benchmark.resolved_workload
    with resolve_model_service(
        benchmark.service,
        retain_runtime_cache=benchmark.profile is not None,
        allow_multiple_models=bool(
            benchmark.trace.trace_selector
            or (workload.dataset_selectors and workload.dataset_selectors != ["random"])
        ),
    ) as service:
        if benchmark.profile is not None and not service.model:
            raise ValueError("--profile requires --model for a multi-model deployment")
        if benchmark.service.health_url:
            asyncio.run(require_health_endpoint(benchmark.service.health_url))
            logger.info("Model service health check passed")
        yield service


@contextmanager
def resolve_model_service(
    source: ModelServiceSource, *, retain_runtime_cache: bool = False,
    allow_multiple_models: bool = False,
) -> Iterator[ModelService]:
    """Yield the model service selected by the benchmark user.

    A URL source is used as given without touching Kubernetes. A Kustomize source
    reuses a complete deployment unchanged, or creates a temporary deployment
    and retires only its own creations afterward; a partially present deployment
    is rejected. Profiling retains RuntimeCache resources and their namespace after
    serving begins, so temporary workload cleanup cannot delete capture output.
    """
    if source.url:
        yield ModelService(
            chat_completions_url=source.url,
            model=source.model,
            api_key=source.api_key,
            models=(source.model,),
            hostname="",
            gpu_count=None,
            routing_host="",
            model_service_refs=(),
        )
        return

    kubectl = Kubectl()
    deployment = load_deployment(source.kustomize_path, kubectl)
    presence = _service_presence(deployment, kubectl)
    if any(presence) and not all(presence):
        raise DeploymentError(
            "The Foretoken deployment is only partially present. "
            "Apply or delete it, then rerun the benchmark"
        )

    lifecycle = DeploymentLifecycle(kubectl, temporary=True)
    temporary = not any(presence)
    serving = False
    try:
        if temporary:
            logger.info("Deploying Foretoken service from %s", deployment.path)
            lifecycle.apply(deployment, source.wait_timeout)
        service = _discover_model_service(
            deployment, kubectl, source, allow_multiple_models=allow_multiple_models
        )
        serving = True
        yield service
    except CaptureCleanupError:
        # Keep participants alive until the controller confirms stop and export.
        logger.error(
            "Capture cleanup is unconfirmed; retaining deployment %s for inspection",
            deployment.path,
        )
        temporary = False
        raise
    finally:
        if temporary:
            logger.info("Cleaning up Foretoken service from %s", deployment.path)
            lifecycle.cleanup_temporary(
                deployment, source.wait_timeout,
                retain_storage=retain_runtime_cache and serving,
            )
