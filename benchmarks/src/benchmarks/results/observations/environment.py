# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Record benchmark client provenance and observed Kubernetes serving conditions."""

from __future__ import annotations

import logging
import platform
import subprocess
import time
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import foretoken
from foretoken.applications.files import ApplicationFiles
from foretoken.deployment.manifest import DEPLOYMENT_UID_LABEL
from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl
from foretoken.platform.config import default_platform_config

from benchmarks.model_service import ModelService

logger = logging.getLogger(__name__)

OBSERVATION_TIMEOUT_SECONDS = 5.0


def client_environment(*, deadline: float | None = None) -> dict[str, Any]:
    """Describe the client within its caller's observation budget, including source when available."""
    if deadline is None:
        deadline = time.monotonic() + OBSERVATION_TIMEOUT_SECONDS
    packages = {}
    for name in ("foretoken", "evalscope", "lm_eval", "httpx", "openai", "transformers", "datasets", "numpy"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    root = Path(foretoken.__file__).resolve().parents[2]
    source = None
    if (root / ".git").exists():
        try:
            commit = subprocess.check_output(
                ["git", "-C", str(root), "rev-parse", "HEAD"], text=True,
                timeout=max(0, deadline - time.monotonic()),
            ).strip()
            changed = subprocess.check_output(
                ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"], text=True,
                timeout=max(0, deadline - time.monotonic()),
            )
            source = {"commit": commit, "dirty": bool(changed)}
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            source = {"error": str(error)}
            logger.warning("Client source observation unavailable: %s", error)
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": packages,
        "source": source,
    }


def serving_environment(service: ModelService, *, deadline: float | None = None) -> dict[str, Any]:
    """Snapshot this service's declared intent and owned runtimes for local before/after evidence.

    URL targets expose no authoritative deployment or hardware inventory. Kubernetes
    read errors are recorded explicitly without failing an otherwise usable endpoint.
    No Secret data, pod environment variables, or unrelated workloads are exported.
    """
    snapshot: dict[str, Any] = {
        "observed_at": datetime.now(UTC).isoformat(),
        "model": service.model,
        "source": "kustomize" if service.model_service_refs else "url",
        "declared_gpu_count": service.gpu_count if service.model_service_refs else None,
    }
    if not service.model_service_refs:
        return snapshot
    snapshot["namespace"] = service.model_service_refs[0].namespace
    if deadline is None:
        deadline = time.monotonic() + OBSERVATION_TIMEOUT_SECONDS
    kubectl = service.kubectl
    try:
        with kubectl.until(deadline):
            snapshot["context"] = kubectl.context
            resources = kubectl.list_resources(
                ("modelservices", "frontendservices", "modelpools", "modelgroups", "deployments", "replicasets", "pods"),
                snapshot["namespace"],
            )
            names = {ref.name for ref in service.model_service_refs}
            models = [r for r in resources if r["kind"] == "ModelService" and r["metadata"]["name"] in names]
            frontends = [r for r in resources if r["kind"] == "FrontendService"
                         and service.deployment is not None and r["metadata"]["name"] == service.deployment.frontend]
            # Namespace membership does not establish ownership of another instance's controller.
            deployment_uid = service.deployment.uid if service.deployment is not None else ""
            controllers = [r for r in resources if r["kind"] == "Deployment"
                           and deployment_uid
                           and r["metadata"].get("labels", {}).get(DEPLOYMENT_UID_LABEL) == deployment_uid
                           and r["metadata"].get("labels", {}).get("app.kubernetes.io/name") == "foretoken-control-plane"]
            owners = {r["metadata"]["uid"] for r in models + frontends + controllers}
            snapshot["controllers"] = [{
                "name": r["metadata"]["name"], "uid": r["metadata"]["uid"],
                "generation": r["metadata"]["generation"],
                "application": {
                    "image": next(c["image"] for c in r["spec"]["template"]["spec"]["containers"] if c["name"] == "manager"),
                    "applicationURL": r["spec"]["template"].get("metadata", {}).get("annotations", {}).get("inference.foretoken.io/application-url", ""),
                },
            } for r in controllers]
            descendants = {}
            # Runtime Pods belong to ReplicaSets created by the group's Deployment.
            for kind in ("ModelPool", "ModelGroup", "Deployment", "ReplicaSet", "Pod"):
                selected = [r for r in resources if r["kind"] == kind and any(
                    owner["uid"] in owners and owner.get("controller")
                    for owner in r["metadata"].get("ownerReferences", [])
                )]
                descendants[kind] = selected
                owners.update(r["metadata"]["uid"] for r in selected)
            snapshot["services"] = [{
                "name": r["metadata"]["name"],
                "uid": r["metadata"]["uid"],
                "generation": r["metadata"]["generation"],
                "observed_generation": r.get("status", {}).get("observedGeneration"),
                "spec": r["spec"],
                "pool_applications": r.get("status", {}).get("poolApplications", {}),
                "serving_pool_revisions": r.get("status", {}).get("servingPoolRevisions", []),
            } for r in models]
            snapshot["frontends"] = [{
                "name": r["metadata"]["name"], "uid": r["metadata"]["uid"],
                "spec": r["spec"], "application": r.get("status", {}).get("application"),
            } for r in frontends]
            snapshot["groups"] = [{
                "name": r["metadata"]["name"],
                "uid": r["metadata"]["uid"],
                "revision": r["spec"]["revision"],
                "phase": r.get("status", {}).get("phase"),
                "artifacts": {key: r["spec"]["artifacts"][key] for key in (
                    "model", "source", "modelRevision", "tokenizer", "tokenizerRevision",
                ) if key in r["spec"]["artifacts"]},
                "runtime": r["spec"]["runtime"],
                "resources": r["spec"]["resources"],
                "parallelism": r["spec"]["parallelism"],
            } for r in descendants["ModelGroup"]]
            # Distributed member Pods are owned by LeaderWorkerSets rather than Deployments.
            # Their launch identity ties them to the already-verified Group UID.
            group_uids = {group["uid"] for group in snapshot["groups"]}
            selected_pods = {pod["metadata"]["uid"]: pod for pod in descendants["Pod"]}
            for pod in resources:
                if pod["kind"] == "Pod" and any(
                    env.get("name") == "FORETOKEN_MODEL_GROUP_UID" and env.get("value") in group_uids
                    for container in pod["spec"]["containers"] if container["name"] == "model-server"
                    for env in container.get("env", [])
                ):
                    selected_pods[pod["metadata"]["uid"]] = pod
            snapshot["pods"] = [{
                "name": r["metadata"]["name"],
                "uid": r["metadata"]["uid"],
                "node": r["spec"].get("nodeName"),
                "phase": r.get("status", {}).get("phase"),
                "application_url": r["metadata"].get("annotations", {}).get("inference.foretoken.io/application-url", ""),
                "containers": [{
                    "name": c["name"], "image": c["image"], "resources": c.get("resources", {}),
                } for c in r["spec"]["containers"]],
                "container_statuses": [{key: c.get(key) for key in (
                    "name", "imageID", "containerID", "ready", "restartCount",
                )} for c in r.get("status", {}).get("containerStatuses", [])],
            } for r in selected_pods.values()]
            snapshot["nodes"] = []
            for name in sorted({p["node"] for p in snapshot["pods"] if p["node"]}):
                node = kubectl.get("node", name)
                snapshot["nodes"].append({
                    "name": name,
                    "node_info": {key: node["status"]["nodeInfo"][key] for key in (
                        "operatingSystem", "architecture", "osImage", "kernelVersion",
                        "containerRuntimeVersion", "kubeletVersion",
                    )},
                    "allocatable": node["status"].get("allocatable", {}),
                })
    except DeploymentError as error:
        snapshot["error"] = str(error)
        logger.warning("Serving environment snapshot incomplete: %s", error)
    return snapshot


class ApplicationSources:
    """Retain build provenance when an application is observed, before retired files can be collected."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.records: dict[str, dict[str, Any]] = {}
        self._archives: dict[str, dict[str, str]] = {}
        self._kubectl: Kubectl | None = None

    def capture(
        self, snapshot: dict[str, Any], *, kubectl: Kubectl | None,
        deadline: float | None = None,
    ) -> None:
        """Read live application provenance with the service's client, deferring archive transfers."""
        if kubectl is not None:
            self._kubectl = kubectl
        references = {
            group["runtime"]["applicationURL"]
            for group in snapshot.get("groups", []) if group["runtime"].get("applicationURL")
        }
        references.update(
            application["applicationURL"]
            for service in snapshot.get("services", [])
            for application in service.get("pool_applications", {}).values() if application.get("applicationURL")
        )
        references.update(
            frontend["application"]["applicationURL"]
            for frontend in snapshot.get("frontends", [])
            if frontend.get("application") and frontend["application"].get("applicationURL")
        )
        references.update(
            controller["application"]["applicationURL"]
            for controller in snapshot.get("controllers", [])
            if controller["application"].get("applicationURL")
        )
        references.update(pod["application_url"] for pod in snapshot.get("pods", []) if pod.get("application_url"))
        if deadline is None:
            deadline = time.monotonic() + OBSERVATION_TIMEOUT_SECONDS
        origin = None
        for reference in sorted(references - self.records.keys()):
            try:
                with self._kubectl.until(deadline):
                    if origin is None:
                        origin = ApplicationFiles(self._kubectl, default_platform_config().namespace)
                    sources = origin.read_sources(reference)
            except DeploymentError as error:
                self.records[reference] = {"sources": None, "error": str(error)}
                continue
            self.records[reference] = {"sources": sources}

    def save_archives(self, *, deadline: float) -> None:
        """Retain source bytes locally at run boundaries, leaving URI evidence when transfer is unavailable."""
        archives_by_application = {
            reference: {
                part["archive"] for part in (record["sources"] or {}).get("parts", {}).values()
                if part is not None and "archive" in part
            }
            for reference, record in self.records.items()
        }
        origin = None
        for archive in sorted({url for urls in archives_by_application.values() for url in urls}):
            if "path" in self._archives.get(archive, {}):
                continue
            try:
                with self._kubectl.until(deadline):
                    if origin is None:
                        origin = ApplicationFiles(self._kubectl, default_platform_config().namespace)
                    path = origin.download_source_archive(
                        archive, self.directory / "sources", timeout=f"{OBSERVATION_TIMEOUT_SECONDS:g}s",
                    )
                self._archives[archive] = {"path": str(path.relative_to(self.directory))}
            except DeploymentError as error:
                self._archives[archive] = {"error": str(error)}
        for reference, archives in archives_by_application.items():
            if archives:
                self.records[reference]["archives"] = {
                    archive: self._archives[archive] for archive in sorted(archives)
                }
