# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Record benchmark client provenance and observed Kubernetes serving conditions."""

from __future__ import annotations

import logging
import platform
import subprocess
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from benchmarks.model_service import ModelService
from foretoken.kubernetes import Kubectl
from foretoken.manifest import DeploymentError

logger = logging.getLogger(__name__)


def client_environment() -> dict[str, Any]:
    """Describe the executing client; a source commit is recorded only for this package's checkout."""
    packages = {}
    for name in ("foretoken", "evalscope", "lm_eval", "httpx", "openai", "transformers", "datasets", "numpy"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    root = Path(__file__).resolve().parents[2]
    source = None
    if (root / ".git").exists():
        commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        changed = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain"], text=True)
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
        "observed_at": datetime.now(timezone.utc).isoformat(),
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
            ("modelservices", "modelpools", "modelgroups", "deployments", "replicasets", "pods"),
            snapshot["namespace"],
        )
        names = {ref.name for ref in service.model_service_refs}
        models = [r for r in resources if r["kind"] == "ModelService" and r["metadata"]["name"] in names]
        owners = {r["metadata"]["uid"] for r in models}
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
        } for r in models]
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
        snapshot["pods"] = [{
            "name": r["metadata"]["name"],
            "uid": r["metadata"]["uid"],
            "node": r["spec"].get("nodeName"),
            "phase": r.get("status", {}).get("phase"),
            "containers": [{
                "name": c["name"], "image": c["image"], "resources": c.get("resources", {}),
            } for c in r["spec"]["containers"]],
            "container_statuses": [{key: c.get(key) for key in (
                "name", "imageID", "ready", "restartCount",
            )} for c in r.get("status", {}).get("containerStatuses", [])],
        } for r in descendants["Pod"]]
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
