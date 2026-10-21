# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Resolve the model service a benchmark measures, including temporary Kustomize deployments."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from foretoken.deployment.endpoint import resolve_frontend_endpoint
from foretoken.deployment.instances import DeploymentInstance, storage_only_instance
from foretoken.deployment.lifecycle import DeploymentLifecycle
from foretoken.deployment.manifest import ForetokenDeployment, load_deployment
from foretoken.deployment.status import wait_for_resources
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl, ResourceRef, timeout_seconds
from foretoken.processes.execution import (
    CommandExecution,
    cleanup_commands,
    run_command,
)
from loguru import logger

from benchmarks.config.benchmark import BenchmarkConfig, ModelServiceSource
from benchmarks.profiling import CaptureCleanupError


@dataclass(frozen=True)
class ModelService:
    """Describe one OpenAI-compatible model service that is ready for benchmark requests.

    ``chat_completions_url`` preserves the public endpoint selected by the user or
    deployment. ``routing_host`` is the HTTP ``Host`` header a Gateway HTTP listener
    needs to route by hostname; it is empty for LoadBalancer, HTTPS, and user-supplied
    URLs. ``model_service_refs`` contains the Kubernetes status sources that serve
    the selected model and is empty for a user-supplied URL. Every client derives
    its ``Authorization`` header from ``api_key``. ``kubectl`` is the command's
    selected client, borrowed by profiling and observation stages.
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
    kubectl: Kubectl | None = None

    @property
    def api_root(self) -> str:
        """Return the OpenAI client base URL from either completion endpoint."""
        return self.chat_completions_url.rstrip("/").removesuffix(
            "/chat/completions"
        ).removesuffix("/completions")

    @property
    def tokenizer_identity(self) -> tuple[str, str]:
        """Return the selected source and client-side tokenizer from the serving cohorts' storage."""
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
        if source == "local":
            from benchmarks.datasets.huggingface import find_local_tokenizer_path

            assert self.kubectl is not None
            directories = {
                cache["directory"]
                for group in _serving_model_groups(self.deployment, self.model, self.kubectl)
                if (cache := group["spec"]["artifacts"].get("cache")) and cache.get("directory")
            }
            for directory in sorted(directories):
                local = find_local_tokenizer_path(tokenizer, data_root=directory)
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
            _model_gpu_count(self.deployment, model, self.kubectl) if model else None
        )
        return replace(self, model=model, models=selected, model_service_refs=refs, gpu_count=gpu_count)

    @property
    def request_headers(self) -> dict[str, str]:
        """Return the routing headers every request must carry, without credentials."""
        return {"Host": self.routing_host} if self.routing_host else {}


class ModelServiceResolver:
    """Resolve one command's services with one lazily selected Kubernetes client."""

    def __init__(self) -> None:
        """Defer cluster selection until a new Kustomize measurement needs it."""
        self._kubectl: Kubectl | None = None

    @contextmanager
    def benchmark(self, benchmark: BenchmarkConfig) -> Iterator[ModelService]:
        """Prepare one performance service while retaining this command's cluster selection."""
        workload = benchmark.resolved_workload
        with self.resolve(
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
    def resolve(
        self, source: ModelServiceSource, *, retain_runtime_cache: bool = False,
        allow_multiple_models: bool = False,
    ) -> Iterator[ModelService]:
        """Yield a URL service or own a temporary Kustomize deployment through cleanup.

        Existing instances are reused unchanged. Every Kustomize source in a
        comparison uses the first Kustomize source's cluster, including observation,
        profiling, and temporary-resource cleanup. URL sources do not access Kubernetes.
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

        execution = CommandExecution.current() or CommandExecution(timeout_seconds(source.wait_timeout))
        with execution:
            if self._kubectl is None:
                self._kubectl = Kubectl()
            kubectl = self._kubectl
            deployment = load_deployment(
                source.kustomize_path, kubectl, name=source.instance_name or None,
            )
            instance = DeploymentInstance(kubectl, deployment.namespace, deployment.name)
            current = instance.read(required=False)
            temporary = current is None or storage_only_instance(current)
            if current is not None:
                if current["metadata"].get("deletionTimestamp"):
                    raise DeploymentError(f"deployment instance {deployment.name!r} is being deleted")
                deployment = instance.bind(deployment) if temporary else instance.deployment()
            elif any(
                kubectl.exists(resource.kind, resource.name, resource.namespace)
                for resource in deployment.service_refs()
            ):
                raise DeploymentError(
                    "The Foretoken deployment is only partially present: services exist without their instance. "
                    "Apply or delete it, then rerun the benchmark"
                )

        lifecycle = DeploymentLifecycle(kubectl, temporary=True)
        serving = False
        try:
            with execution:
                if temporary:
                    logger.info("Deploying Foretoken instance {} from {}", deployment.name, source.kustomize_path)
                    deployment = lifecycle.apply(deployment, source.wait_timeout)
                service = _discover_model_service(
                    deployment, kubectl, source, allow_multiple_models=allow_multiple_models
                )
            serving = True
            yield service
        except CaptureCleanupError:
            # Keep participants alive until the controller confirms stop and export.
            temporary = False
            logger.error(
                "Capture cleanup is unconfirmed; retaining deployment instance {} for inspection",
                deployment.name,
            )
            raise
        finally:
            if temporary:
                logger.info("Cleaning up Foretoken deployment instance {}", deployment.name)
                with cleanup_commands(timeout=timeout_seconds(source.wait_timeout)):
                    lifecycle.cleanup_temporary(
                        deployment, source.wait_timeout,
                        retain_storage=retain_runtime_cache and serving,
                    )


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
    total = sum(
        int(group["spec"]["nodeCount"]) * int(group["spec"]["resources"]["requests"]["gpu"]["count"])
        for group in _serving_model_groups(deployment, model, kubectl)
    )
    if total < 1:
        raise DeploymentError(f"no resolved GPU capacity for model {model!r}")
    return total


def _serving_model_groups(
    deployment: ForetokenDeployment, model: str, kubectl: Kubectl,
) -> Iterator[dict[str, Any]]:
    """Yield ready serving cohorts for capacity and artifact discovery, excluding retired revisions."""
    services = kubectl.get_resources(_model_service_refs(deployment, model))
    selected = {
        (pool["poolUID"], pool["revision"])
        for service in services
        for pool in service["status"]["servingPoolRevisions"]
    }
    for group in kubectl.list_resources(("modelgroups",), deployment.namespace):
        spec = group["spec"]
        if (spec["modelPoolRef"]["uid"], spec["revision"]) not in selected:
            continue
        if group["metadata"].get("deletionTimestamp") or group["status"].get("phase") != "Ready":
            continue
        yield group


def _wait_for_models(
    api_root: str,
    headers: dict[str, str],
    deadline: float,
    expected_models: Iterable[str],
    api_key: str,
) -> tuple[str, ...]:
    """Observe the public model list within one deadline, including DNS and response-body transfer."""
    expected = set(expected_models)
    request = json.dumps({
        "url": f"{api_root}/models",
        "headers": {**headers, "Authorization": f"Bearer {api_key}"},
    })
    # Socket timeouts do not bound DNS or a continuously trickling response.
    # The command owner can terminate the entire request; credentials use stdin.
    query = """import json, sys, httpx
try:
    response = httpx.get(**json.load(sys.stdin), timeout=5.0, follow_redirects=True)
    response.raise_for_status()
    print(response.text)
except httpx.HTTPError as error:
    print(str(error) or type(error).__name__, file=sys.stderr)
    sys.exit(2)
"""
    last_error = "frontend has not responded"
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            result = run_command(
                [sys.executable, "-I", "-c", query], input=request,
                text=True, capture_output=True, timeout=remaining,
            )
        except DeploymentError:
            if execution := CommandExecution.current():
                execution.check()
            last_error = "frontend model request exceeded its wait budget"
        else:
            if result.returncode:
                if execution := CommandExecution.current():
                    execution.check()
                last_error = result.stderr.strip() or f"frontend model request exited with status {result.returncode}"
                if result.returncode != 2:
                    raise DeploymentError(f"frontend model query failed: {last_error}")
            else:
                try:
                    models = tuple(sorted(
                        str(item["id"])
                        for item in json.loads(result.stdout).get("data", [])
                        if isinstance(item, dict) and item.get("id")
                    ))
                except (ValueError, KeyError) as error:
                    last_error = str(error)
                else:
                    if expected.issubset(models):
                        return models
                    last_error = (
                        f"frontend advertises {models or '<no models>'}; "
                        f"waiting for {tuple(sorted(expected))}"
                    )
        if execution := CommandExecution.current():
            execution.check()
        time.sleep(min(2, max(0.0, deadline - time.monotonic())))
    raise DeploymentError(f"frontend model discovery deadline exceeded: {last_error}")


def _discover_model_service(
    deployment: ForetokenDeployment,
    kubectl: Kubectl,
    source: ModelServiceSource,
    *,
    allow_multiple_models: bool = False,
) -> ModelService:
    """Wait for the selected instance's services and return their public model endpoint."""
    model = (
        "" if allow_multiple_models and not source.model and len(set(deployment.models.values())) > 1
        else _select_model(deployment.models.values(), source.model)
    )
    model_refs = _model_service_refs(deployment, model)
    resources = (ResourceRef("FrontendService", deployment.frontend, deployment.namespace), *model_refs)
    with kubectl.until(time.monotonic() + timeout_seconds(source.wait_timeout)) as deadline:
        progress = wait_for_resources(resources, kubectl, source.wait_timeout)
        for service in progress:
            if service.state == "Stopped":
                command = f"foretoken deploy {shlex.quote(source.kustomize_path)}"
                if source.instance_name:
                    command += f" --name {shlex.quote(source.instance_name)}"
                raise DeploymentError(
                    f"{service.resource.display_name} is stopped; resume it before benchmarking with:\n"
                    f"{command}"
                )
        gpu_count = _model_gpu_count(deployment, model, kubectl) if model else None
        endpoint = resolve_frontend_endpoint(deployment, kubectl, source.wait_timeout)
        chat_completions_url = f"{endpoint.url}/v1/chat/completions"
        api_root = f"{endpoint.url}/v1"
        headers = {"Host": endpoint.routing_host} if endpoint.routing_host else {}
        models = _wait_for_models(
            api_root,
            headers,
            deadline,
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
        kubectl=kubectl,
    )
