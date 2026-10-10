# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Record benchmark client provenance and observed Kubernetes serving conditions."""

from __future__ import annotations

import logging
import platform
import subprocess
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import foretoken
from foretoken.application_files import ApplicationFiles
from foretoken.kubernetes import Kubectl
from foretoken.manifest import DeploymentError
from foretoken.platform.config import default_platform_config

from benchmarks.model_service import ModelService

logger = logging.getLogger(__name__)


def client_environment() -> dict[str, Any]:
    """Describe the executing client; a source commit is recorded only for this package's checkout."""
    packages = {}
    for name in ("foretoken", "evalscope", "lm_eval", "httpx", "openai", "transformers", "datasets", "numpy"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    root = Path(foretoken.__file__).resolve().parents[2]
    source = None
    if (root / ".git").exists():
        commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        changed = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"], text=True)
        source = {"commit": commit, "dirty": bool(changed)}
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": packages,
        "source": source,
    }


def serving_environment(service: ModelService) -> dict[str, Any]:
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
    kubectl = Kubectl()
    try:
        snapshot["context"] = kubectl.current_context()
        kubectl = Kubectl(context=snapshot["context"])
        resources = kubectl.list_resources(
            ("modelservices", "frontendservices", "modelpools", "modelgroups", "deployments", "replicasets", "pods"),
            snapshot["namespace"],
        )
        names = {ref.name for ref in service.model_service_refs}
        models = [r for r in resources if r["kind"] == "ModelService" and r["metadata"]["name"] in names]
        frontends = [r for r in resources if r["kind"] == "FrontendService"
                     and service.deployment is not None and r["metadata"]["name"] == service.deployment.frontend]
        owners = {r["metadata"]["uid"] for r in models + frontends}
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

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self._origin: ApplicationFiles | None = None

    def capture(self, snapshot: dict[str, Any]) -> None:
        """Capture newly observed service applications for the run-owned resource observer."""
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
        for reference in sorted(references - self.records.keys()):
            try:
                if self._origin is None:
                    self._origin = ApplicationFiles(
                        Kubectl(context=snapshot.get("context")), default_platform_config().namespace,
                    )
                self.records[reference] = {"sources": self._origin.read_sources(reference)}
            except DeploymentError as error:
                self.records[reference] = {"sources": None, "error": str(error)}
