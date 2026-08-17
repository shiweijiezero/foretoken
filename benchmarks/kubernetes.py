# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Kubernetes orchestration for deploy-and-benchmark runs."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from benchmarks.arguments import CleanupCommand, RunCommand
from benchmarks.storage.result_writer import (
    validate_run_id,
    write_json_atomic,
)

logger = logging.getLogger(__name__)

_MANAGED_BY = "foretoken-bench"
_MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
_RUN_ID_LABEL = "inference.foretoken.io/benchmark-run"
_ALLOWED_DEPLOY_KINDS = {"FrontendService", "ModelService", "Namespace"}
_REQUIRED_CRDS = (
    "frontendservices.inference.foretoken.io",
    "modelservices.inference.foretoken.io",
)
_FRONTEND_SYNC_SCRIPT = """
import json
import os
import time
import urllib.request

url = os.environ["FORETOKEN_FRONTEND_STATUS_URL"]
expected = int(os.environ["FORETOKEN_FRONTEND_GENERATION"])
deadline = time.monotonic() + int(os.environ["FORETOKEN_FRONTEND_WAIT_SECONDS"])
last_error = "frontend has not loaded the expected serving snapshot"
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            status = json.load(response)
        if status.get("serving_ready") and int(status.get("active_generation") or 0) >= expected:
            raise SystemExit(0)
        last_error = f"frontend status is {status}"
    except Exception as exc:
        last_error = str(exc)
    time.sleep(1)
raise SystemExit(f"frontend did not become ready: {last_error}")
""".strip()


class KubernetesBenchmarkError(RuntimeError):
    pass


class Kubectl:
    def __init__(self) -> None:
        if shutil.which("kubectl") is None:
            raise KubernetesBenchmarkError("kubectl is required for --deploy runs")

    def run(
        self,
        args: Iterable[str],
        *,
        input_text: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = ["kubectl", *args]
        completed = subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )
        if check and completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise KubernetesBenchmarkError(f"{' '.join(command)} failed: {detail}")
        return completed

    def json(self, args: Iterable[str]) -> dict[str, Any]:
        output = self.run([*args, "-o", "json"]).stdout
        return json.loads(output)

    def apply(self, documents: list[dict[str, Any]]) -> None:
        rendered = yaml.safe_dump_all(
            documents,
            sort_keys=False,
            explicit_start=True,
        )
        self.run(["apply", "--server-side", "-f", "-"], input_text=rendered)

    def save_logs(self, namespace: str, job: str, destination: Path) -> None:
        result = self.run(
            ["logs", "--namespace", namespace, f"job/{job}"],
            check=False,
        )
        output = result.stdout + result.stderr
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(output, encoding="utf-8")
        if result.stdout:
            print(result.stdout, end="")


def _labels(run_id: str) -> dict[str, str]:
    return {
        _MANAGED_BY_LABEL: _MANAGED_BY,
        _RUN_ID_LABEL: run_id,
    }


def _name(run_id: str, suffix: str) -> str:
    available = 63 - len(suffix) - 1
    return f"{run_id[:available].rstrip('-')}-{suffix}"


def _load_deployment(path_value: str, kubectl: Kubectl) -> list[dict[str, Any]]:
    path = Path(path_value).expanduser().resolve()
    if path.is_dir():
        rendered = kubectl.run(["kustomize", str(path)]).stdout
    elif path.is_file():
        rendered = path.read_text(encoding="utf-8")
    else:
        raise KubernetesBenchmarkError(f"deployment path not found: {path}")

    try:
        rendered_documents = list(yaml.safe_load_all(rendered))
    except yaml.YAMLError as exc:
        raise KubernetesBenchmarkError(f"invalid deployment YAML: {exc}") from exc

    documents: list[dict[str, Any]] = []
    for index, document in enumerate(rendered_documents, start=1):
        if document is None:
            continue
        if not isinstance(document, dict):
            raise KubernetesBenchmarkError(
                f"deployment document {index} must be a Kubernetes object"
            )
        kind = document.get("kind")
        if kind not in _ALLOWED_DEPLOY_KINDS:
            raise KubernetesBenchmarkError(
                "--deploy accepts only Namespace, FrontendService, and "
                f"ModelService; document {index} has kind {kind!r}"
            )
        expected_api_version = (
            "v1" if kind == "Namespace" else "inference.foretoken.io/v1alpha1"
        )
        if document.get("apiVersion") != expected_api_version:
            raise KubernetesBenchmarkError(
                f"deployment document {index} ({kind}) must use "
                f"apiVersion {expected_api_version}"
            )
        documents.append(document)
    if not documents:
        raise KubernetesBenchmarkError("deployment rendered no Kubernetes resources")
    return documents


def _prepare_services(
    documents: list[dict[str, Any]],
    *,
    namespace: str,
    run_id: str,
    requested_model: str,
) -> tuple[list[dict[str, Any]], str, str, list[str]]:
    services: list[dict[str, Any]] = []
    frontends: list[str] = []
    models: dict[str, str] = {}
    labels = _labels(run_id)

    for document in documents:
        if document.get("kind") == "Namespace":
            continue
        metadata = document.setdefault("metadata", {})
        metadata["namespace"] = namespace
        metadata.setdefault("labels", {}).update(labels)
        name = str(metadata.get("name", "")).strip()
        if not name:
            raise KubernetesBenchmarkError("deployed resources require metadata.name")
        kind = document.get("kind")
        if kind == "FrontendService":
            frontends.append(name)
        elif kind == "ModelService":
            model = str(document.get("spec", {}).get("model", "")).strip()
            if not model:
                raise KubernetesBenchmarkError(
                    f"ModelService {name} does not define spec.model"
                )
            models[name] = model
        services.append(document)

    if len(frontends) != 1:
        raise KubernetesBenchmarkError(
            "--deploy must contain exactly one FrontendService"
        )
    if not models:
        raise KubernetesBenchmarkError(
            "--deploy must contain at least one ModelService"
        )

    if requested_model:
        if requested_model not in models.values():
            raise KubernetesBenchmarkError(
                f"model {requested_model!r} is not present in --deploy resources"
            )
        model = requested_model
    elif len(models) == 1:
        model = next(iter(models.values()))
    else:
        raise KubernetesBenchmarkError(
            "--model is required when --deploy contains multiple ModelServices"
        )
    selected_services = [name for name, value in models.items() if value == model]
    return services, frontends[0], model, selected_services


def _namespace(run_id: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": run_id, "labels": _labels(run_id)},
    }


def _resolve_storage_class(kubectl: Kubectl, requested: str) -> str:
    data = kubectl.json(["get", "storageclass"])
    items = data.get("items", [])
    names = {item["metadata"]["name"] for item in items}
    if requested:
        if requested not in names:
            raise KubernetesBenchmarkError(f"StorageClass {requested!r} does not exist")
        return requested

    defaults = []
    for item in items:
        annotations = item.get("metadata", {}).get("annotations", {})
        if (
            annotations.get("storageclass.kubernetes.io/is-default-class") == "true"
            or annotations.get("storageclass.beta.kubernetes.io/is-default-class")
            == "true"
        ):
            defaults.append(item["metadata"]["name"])
    if len(defaults) != 1:
        raise KubernetesBenchmarkError(
            "--deploy requires one default StorageClass or --storage-class"
        )
    return defaults[0]


def _pvc(run_id: str, namespace: str, storage_class: str, size: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": _name(run_id, "results"),
            "namespace": namespace,
            "labels": _labels(run_id),
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": storage_class,
            "resources": {"requests": {"storage": size}},
        },
    }


def _workload_config_map(
    run_id: str, namespace: str, path_value: str
) -> dict[str, Any]:
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise KubernetesBenchmarkError(f"dataset not found: {path}")
    if path.stat().st_size > 900 * 1024:
        raise KubernetesBenchmarkError(
            "managed mode currently supports JSONL files up to 900 KiB"
        )
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": _name(run_id, "workload"),
            "namespace": namespace,
            "labels": _labels(run_id),
        },
        "data": {"workload.jsonl": path.read_text(encoding="utf-8")},
    }


def _runner_args(
    command: RunCommand,
    *,
    run_id: str,
    base_url: str,
    model: str,
) -> list[str]:
    args = [
        "bench",
        "run",
        "--base-url",
        base_url,
        "--model",
        model,
        "--num-requests",
        str(command.num_requests),
        "--max-concurrency",
        str(command.max_concurrency),
        "--request-rate",
        str(command.request_rate),
        "--timeout",
        str(command.timeout),
        "--max-retries",
        str(command.max_retries),
        "--dataset-offset",
        str(command.dataset_offset),
        "--max-tokens",
        str(command.max_tokens),
        "--temperature",
        str(command.temperature),
        "--output-dir",
        "/results",
        "--run-id",
        run_id,
        "--execution-context",
        "managed",
    ]
    if command.open_loop:
        args.append("--open-loop")
    if command.apply_chat_template is not None:
        args.append(
            "--apply-chat-template"
            if command.apply_chat_template
            else "--no-apply-chat-template"
        )
    if command.max_turns is not None:
        args.extend(["--max-turns", str(command.max_turns)])
    args.append("--stream" if command.stream else "--no-stream")
    if command.prompt:
        args.extend(["--prompt", command.prompt])
    else:
        args.extend(["--dataset-path", "/workload/workload.jsonl"])
    return args


def _job(
    command: RunCommand,
    *,
    run_id: str,
    namespace: str,
    frontend: str,
    frontend_port: int,
    model: str,
    pvc_name: str,
    config_map_name: str | None,
    serving_snapshot_version: int,
) -> dict[str, Any]:
    job_name = _name(run_id, "job")
    volumes: list[dict[str, Any]] = [
        {
            "name": "results",
            "persistentVolumeClaim": {"claimName": pvc_name},
        },
        {"name": "tmp", "emptyDir": {}},
    ]
    mounts: list[dict[str, Any]] = [
        {"name": "results", "mountPath": "/results"},
        {"name": "tmp", "mountPath": "/tmp"},
    ]
    if config_map_name:
        volumes.append({"name": "workload", "configMap": {"name": config_map_name}})
        mounts.append({"name": "workload", "mountPath": "/workload", "readOnly": True})

    service_root = (
        f"http://{frontend}.{namespace}.svc.cluster.local:{frontend_port}"
    )
    base_url = f"{service_root}/v1"
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": namespace,
            "labels": _labels(run_id),
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": command.job_timeout,
            "template": {
                "metadata": {"labels": _labels(run_id)},
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 65532,
                        "runAsGroup": 65532,
                        "fsGroup": 65532,
                    },
                    "initContainers": [
                        {
                            "name": "wait-for-frontend",
                            "image": command.benchmark_image,
                            "command": ["python", "-c", _FRONTEND_SYNC_SCRIPT],
                            "env": [
                                {
                                    "name": "FORETOKEN_FRONTEND_STATUS_URL",
                                    "value": f"{service_root}/statusz",
                                },
                                {
                                    "name": "FORETOKEN_FRONTEND_GENERATION",
                                    "value": str(serving_snapshot_version),
                                },
                                {
                                    "name": "FORETOKEN_FRONTEND_WAIT_SECONDS",
                                    "value": str(
                                        min(
                                            command.service_timeout,
                                            command.job_timeout,
                                        )
                                    ),
                                },
                            ],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                    "containers": [
                        {
                            "name": "benchmark",
                            "image": command.benchmark_image,
                            "args": _runner_args(
                                command,
                                run_id=run_id,
                                base_url=base_url,
                                model=model,
                            ),
                            "volumeMounts": mounts,
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                    "volumes": volumes,
                },
            },
        },
    }


def _wait_ready(
    kubectl: Kubectl,
    *,
    namespace: str,
    resource: str,
    timeout: int,
) -> None:
    kubectl.run(
        [
            "wait",
            "--namespace",
            namespace,
            resource,
            "--for=condition=Ready",
            f"--timeout={timeout}s",
        ]
    )


def _wait_routable_snapshot(
    kubectl: Kubectl,
    *,
    namespace: str,
    frontend: str,
    model: str,
    timeout: int,
) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        config_maps = kubectl.json(
            [
                "get",
                "configmaps",
                "--namespace",
                namespace,
                "--selector",
                f"inference.foretoken.io/frontend-service={frontend}",
            ]
        )
        for config_map in config_maps.get("items", []):
            payload = config_map.get("data", {}).get("serving.json")
            if not payload:
                continue
            try:
                snapshot = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if any(group.get("model") == model for group in snapshot.get("groups", [])):
                version = snapshot.get("version")
                if isinstance(version, int) and version > 0:
                    return version
        time.sleep(1)
    raise KubernetesBenchmarkError(
        f"frontend {frontend} did not publish a routable snapshot for model {model!r} "
        f"within {timeout} seconds"
    )


def _frontend_port(kubectl: Kubectl, namespace: str, frontend: str) -> int:
    service = kubectl.json(["get", "service", frontend, "--namespace", namespace])
    ports = service.get("spec", {}).get("ports", [])
    http_ports = [port for port in ports if port.get("name") == "http"]
    if len(http_ports) != 1 or not isinstance(http_ports[0].get("port"), int):
        raise KubernetesBenchmarkError(
            f"frontend Service {frontend} must expose one named http port"
        )
    return int(http_ports[0]["port"])


def _job_startup_blocker(
    kubectl: Kubectl, namespace: str, job: str
) -> tuple[str, str] | None:
    pods = kubectl.json(
        [
            "get",
            "pods",
            "--namespace",
            namespace,
            "--selector",
            f"job-name={job}",
        ]
    )
    for pod in pods.get("items", []):
        status = pod.get("status", {})
        for condition in status.get("conditions", []):
            if (
                condition.get("type") == "PodScheduled"
                and condition.get("status") == "False"
            ):
                return (
                    str(condition.get("reason") or "Unschedulable"),
                    str(condition.get("message") or ""),
                )
        for container_status in status.get("containerStatuses", []):
            waiting = container_status.get("state", {}).get("waiting")
            if waiting and waiting.get("reason") in {
                "InvalidImageName",
                "CreateContainerConfigError",
                "ErrImagePull",
                "ImagePullBackOff",
            }:
                return (
                    str(waiting["reason"]),
                    str(waiting.get("message") or ""),
                )
    return None


def _wait_job(
    kubectl: Kubectl,
    *,
    namespace: str,
    job: str,
    timeout: int,
) -> tuple[bool, dict[str, Any]]:
    deadline = time.monotonic() + timeout
    blocked_at: float | None = None
    blocker_category: str | None = None
    while time.monotonic() < deadline:
        data = kubectl.json(["get", "job", job, "--namespace", namespace])
        for condition in data.get("status", {}).get("conditions", []):
            if condition.get("status") != "True":
                continue
            if condition.get("type") == "Complete":
                return True, data
            if condition.get("type") == "Failed":
                return False, data

        blocker = _job_startup_blocker(kubectl, namespace, job)
        if blocker:
            reason, message = blocker
            if reason in {"InvalidImageName", "CreateContainerConfigError"}:
                detail = f": {message}" if message else ""
                raise KubernetesBenchmarkError(
                    f"benchmark Job {job} cannot start: {reason}{detail}"
                )
            category = (
                "image-pull"
                if reason in {"ErrImagePull", "ImagePullBackOff"}
                else reason
            )
            if blocker_category != category:
                blocker_category = category
                blocked_at = time.monotonic()
            if blocked_at is not None and time.monotonic() - blocked_at >= 120:
                detail = f": {message}" if message else ""
                raise KubernetesBenchmarkError(
                    f"benchmark Job {job} remains blocked: {reason}{detail}"
                )
        else:
            blocked_at = None
            blocker_category = None
        time.sleep(5)
    raise KubernetesBenchmarkError(
        f"benchmark Job {job} did not finish within {timeout} seconds"
    )


def _job_pod(
    kubectl: Kubectl, *, namespace: str, job: str, run_id: str
) -> dict[str, Any]:
    data = kubectl.json(
        [
            "get",
            "pods",
            "--namespace",
            namespace,
            "--selector",
            f"job-name={job},{_RUN_ID_LABEL}={run_id}",
        ]
    )
    items = data.get("items", [])
    if len(items) != 1:
        raise KubernetesBenchmarkError(
            f"expected one benchmark Pod for run {run_id}, found {len(items)}"
        )
    return items[0]


def _transfer_pod(
    command: RunCommand,
    *,
    run_id: str,
    namespace: str,
    pvc_name: str,
    node_name: str,
) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": _name(run_id, "transfer"),
            "namespace": namespace,
            "labels": _labels(run_id),
        },
        "spec": {
            "nodeName": node_name,
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 65532,
                "runAsGroup": 65532,
                "fsGroup": 65532,
            },
            "containers": [
                {
                    "name": "transfer",
                    "image": command.benchmark_image,
                    "command": ["sh", "-c", "sleep 3600"],
                    "volumeMounts": [
                        {
                            "name": "results",
                            "mountPath": "/results",
                            "readOnly": True,
                        }
                    ],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                }
            ],
            "volumes": [
                {
                    "name": "results",
                    "persistentVolumeClaim": {"claimName": pvc_name},
                }
            ],
        },
    }


def _collect_results(
    kubectl: Kubectl,
    command: RunCommand,
    *,
    run_id: str,
    namespace: str,
    job: str,
    pvc_name: str,
    local_dir: Path,
) -> None:
    pod = _job_pod(
        kubectl,
        namespace=namespace,
        job=job,
        run_id=run_id,
    )
    node_name = str(pod.get("spec", {}).get("nodeName", ""))
    if not node_name:
        raise KubernetesBenchmarkError("benchmark Pod was never scheduled")

    transfer = _transfer_pod(
        command,
        run_id=run_id,
        namespace=namespace,
        pvc_name=pvc_name,
        node_name=node_name,
    )
    transfer_name = transfer["metadata"]["name"]
    kubectl.apply([transfer])
    try:
        kubectl.run(
            [
                "wait",
                "--namespace",
                namespace,
                f"pod/{transfer_name}",
                "--for=condition=Ready",
                "--timeout=300s",
            ]
        )
        completed = subprocess.run(
            [
                "kubectl",
                "cp",
                f"{namespace}/{transfer_name}:/results/{run_id}/.",
                str(local_dir),
                "--container=transfer",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            raise KubernetesBenchmarkError(
                "failed to collect benchmark results: "
                f"{completed.stderr.strip() or completed.stdout.strip()}"
            )
    finally:
        kubectl.run(
            [
                "delete",
                "pod",
                transfer_name,
                "--namespace",
                namespace,
                "--wait=false",
            ],
            check=False,
        )

    for filename in ("config.json", "raw-output.json", "metrics.json"):
        path = local_dir / filename
        if not path.is_file():
            raise KubernetesBenchmarkError(
                f"managed benchmark did not produce {filename}"
            )
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise KubernetesBenchmarkError(
                f"managed benchmark produced invalid {filename}: {exc}"
            ) from exc


def _write_diagnostics(
    kubectl: Kubectl,
    *,
    namespace: str,
    job: str,
    local_dir: Path,
) -> None:
    logs = local_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    kubectl.save_logs(namespace, job, logs / "benchmark.log")
    for filename, args in (
        ("job.txt", ["describe", "job", job, "--namespace", namespace]),
        (
            "pods.txt",
            [
                "get",
                "pods",
                "--namespace",
                namespace,
                "-o",
                "wide",
            ],
        ),
        (
            "events.txt",
            [
                "get",
                "events",
                "--namespace",
                namespace,
                "--sort-by=.lastTimestamp",
            ],
        ),
    ):
        result = kubectl.run(args, check=False)
        (logs / filename).write_text(
            result.stdout + result.stderr,
            encoding="utf-8",
        )


def _update_manifest(path: Path, **changes: Any) -> None:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.update(changes)
    write_json_atomic(path, manifest)


def run_managed(command: RunCommand, run_id: str) -> dict[str, Any]:
    validate_run_id(run_id)
    if not command.benchmark_image:
        raise KubernetesBenchmarkError(
            "--benchmark-image or FORETOKEN_BENCHMARK_IMAGE is required with --deploy"
        )
    if command.service_timeout < 1:
        raise KubernetesBenchmarkError("--service-timeout must be at least 1 second")
    if command.job_timeout < 1:
        raise KubernetesBenchmarkError("--job-timeout must be at least 1 second")
    if len(command.datasets) > 1:
        raise KubernetesBenchmarkError(
            "managed mode supports one local JSONL dataset per run"
        )
    kubectl = Kubectl()
    for crd in _REQUIRED_CRDS:
        kubectl.run(["get", "crd", crd])
    storage_class = _resolve_storage_class(kubectl, command.storage_class)
    documents = _load_deployment(command.deploy, kubectl)
    services, frontend, model, model_services = _prepare_services(
        documents,
        namespace=run_id,
        run_id=run_id,
        requested_model=command.model,
    )
    pvc_name = _name(run_id, "results")
    resources: list[dict[str, Any]] = [
        _pvc(run_id, run_id, storage_class, command.results_size)
    ]
    config_map_name: str | None = None
    if command.dataset_path:
        config_map = _workload_config_map(
            run_id,
            run_id,
            command.dataset_path,
        )
        config_map_name = config_map["metadata"]["name"]
        resources.append(config_map)

    local_dir = Path(command.output_dir) / run_id
    local_dir.mkdir(parents=True, exist_ok=False)
    manifest_path = local_dir / "manifest.json"
    write_json_atomic(
        manifest_path,
        {
            "run_id": run_id,
            "execution_context": "managed",
            "resources_owned": True,
            "namespace": run_id,
            "phase": "preparing",
            "retained": False,
            "artifacts_collected": False,
        },
    )

    namespace_created = False
    job_name = _name(run_id, "job")
    try:
        existing = kubectl.run(["get", "namespace", run_id], check=False)
        if existing.returncode == 0:
            raise KubernetesBenchmarkError(
                f"namespace {run_id} already exists; choose another --name"
            )
        kubectl.apply([_namespace(run_id)])
        namespace_created = True
        _update_manifest(manifest_path, phase="deploying")
        kubectl.apply(resources)
        kubectl.apply(services)
        for service in model_services:
            _wait_ready(
                kubectl,
                namespace=run_id,
                resource=f"modelservice/{service}",
                timeout=command.service_timeout,
            )
        _wait_ready(
            kubectl,
            namespace=run_id,
            resource=f"frontendservice/{frontend}",
            timeout=command.service_timeout,
        )
        serving_snapshot_version = _wait_routable_snapshot(
            kubectl,
            namespace=run_id,
            frontend=frontend,
            model=model,
            timeout=command.service_timeout,
        )
        frontend_port = _frontend_port(kubectl, run_id, frontend)

        job = _job(
            command,
            run_id=run_id,
            namespace=run_id,
            frontend=frontend,
            frontend_port=frontend_port,
            model=model,
            pvc_name=pvc_name,
            config_map_name=config_map_name,
            serving_snapshot_version=serving_snapshot_version,
        )
        kubectl.apply([job])
        _update_manifest(
            manifest_path,
            phase="running",
            job=job_name,
            model=model,
            frontend=frontend,
        )
        logger.info("Waiting for benchmark Job %s", job_name)
        succeeded, _ = _wait_job(
            kubectl,
            namespace=run_id,
            job=job_name,
            timeout=command.job_timeout,
        )
        _write_diagnostics(
            kubectl,
            namespace=run_id,
            job=job_name,
            local_dir=local_dir,
        )
        _collect_results(
            kubectl,
            command,
            run_id=run_id,
            namespace=run_id,
            job=job_name,
            pvc_name=pvc_name,
            local_dir=local_dir,
        )
        _update_manifest(
            manifest_path,
            run_id=run_id,
            execution_context="managed",
            resources_owned=True,
            namespace=run_id,
            job=job_name,
            model=model,
            frontend=frontend,
            phase="completed" if succeeded else "failed",
            artifacts_collected=True,
            retained=(not succeeded or command.keep),
        )
        if not succeeded:
            raise KubernetesBenchmarkError(
                f"benchmark Job failed; resources retained in namespace {run_id}"
            )
        if command.keep:
            logger.info("Resources retained in namespace %s", run_id)
        else:
            kubectl.run(["delete", "namespace", run_id, "--wait=true", "--timeout=10m"])
            namespace_created = False
            _update_manifest(
                manifest_path,
                phase="cleaned",
                retained=False,
            )
        return {
            "run_id": run_id,
            "output_dir": str(local_dir),
            "namespace": run_id,
        }
    except Exception:
        if namespace_created:
            _write_diagnostics(
                kubectl,
                namespace=run_id,
                job=job_name,
                local_dir=local_dir,
            )
            _update_manifest(
                manifest_path,
                run_id=run_id,
                execution_context="managed",
                resources_owned=True,
                namespace=run_id,
                phase="failed",
                retained=True,
            )
            logger.error(
                "Resources retained for debugging. Cleanup: foretoken bench cleanup %s --output-dir %s",
                run_id,
                command.output_dir,
            )
        else:
            _update_manifest(manifest_path, phase="failed", retained=False)
        raise


def cleanup_managed(command: CleanupCommand) -> None:
    run_id = validate_run_id(command.run_id)
    manifest_path = Path(command.output_dir) / run_id / "manifest.json"
    if not manifest_path.is_file():
        raise KubernetesBenchmarkError(f"run manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("run_id") != run_id
        or manifest.get("namespace") != run_id
        or manifest.get("resources_owned") is not True
    ):
        raise KubernetesBenchmarkError(
            "run manifest does not identify CLI-owned Kubernetes resources"
        )

    kubectl = Kubectl()
    existing = kubectl.run(["get", "namespace", run_id], check=False)
    if existing.returncode:
        _update_manifest(manifest_path, phase="cleaned", retained=False)
        return
    namespace = kubectl.json(["get", "namespace", run_id])
    labels = namespace.get("metadata", {}).get("labels", {})
    if (
        labels.get(_MANAGED_BY_LABEL) != _MANAGED_BY
        or labels.get(_RUN_ID_LABEL) != run_id
    ):
        raise KubernetesBenchmarkError(
            f"namespace {run_id} is not owned by this benchmark run"
        )
    kubectl.run(["delete", "namespace", run_id, "--wait=true", "--timeout=10m"])
    _update_manifest(manifest_path, phase="cleaned", retained=False)
