# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Retire platform-owned admission stores through StatefulSet PVC ownership and native GC."""

from __future__ import annotations

import json
import time
from typing import Any

from foretoken.errors import DeploymentError
from foretoken.kubernetes.client import Kubectl, timeout_seconds
from foretoken.platform.helm_client import owned_by_release
from foretoken.platform.types import ReleaseRef


class AdmissionStoreRetirement:
    """Preserve verified PVC identities across platform removal and interrupted uninstall retries."""

    def __init__(self, kubectl: Kubectl, release: ReleaseRef) -> None:
        self._kubectl = kubectl
        self._release = release

    def claims(self) -> tuple[dict[str, Any], ...]:
        """Find verified cleanup receipts, including after Helm has removed the platform."""
        if not self._kubectl.exists("namespace", self._release.namespace):
            return ()
        return tuple(
            claim["metadata"] for claim in self._kubectl.list_resources(
                ("persistentvolumeclaims",), self._release.namespace,
                label_selector="app.kubernetes.io/name=foretoken-admission-store",
            )
            if owned_by_release(claim, self._release)
        )

    def prepare(self, timeout: str) -> tuple[dict[str, Any], ...]:
        """Stop owned stores and return PVC UID receipts for the platform's post-uninstall wait."""
        kubectl = self._kubectl
        release = self._release
        claims_to_wait = []
        for store in kubectl.list_resources(
            ("statefulsets",), release.namespace,
            label_selector="app.kubernetes.io/name=foretoken-admission-store",
        ):
            if not owned_by_release(store, release):
                continue
            metadata = store["metadata"]
            name, uid = metadata["name"], metadata["uid"]
            labels = store["spec"]["selector"]["matchLabels"]
            selector = ",".join(f"{key}={value}" for key, value in labels.items())

            def controlled(document: dict[str, Any], uid: str = uid) -> bool:
                return any(
                    owner.get("controller") and owner["uid"] == uid
                    for owner in document["metadata"].get("ownerReferences", [])
                )

            pods = [
                pod for pod in kubectl.list_resources(("pods",), release.namespace, label_selector=selector)
                if controlled(pod)
            ]
            templates = {template["metadata"]["name"] for template in store["spec"].get("volumeClaimTemplates", [])}
            mounted = {
                volume["persistentVolumeClaim"]["claimName"] for pod in pods
                for volume in pod["spec"].get("volumes", [])
                if volume["name"] in templates and "persistentVolumeClaim" in volume
            }
            owners = {uid, *(pod["metadata"]["uid"] for pod in pods)}

            def validate_claim(
                claim: dict[str, Any], labels: dict[str, str] = labels, owners: set[str] = owners,
            ) -> None:
                """Reject conflicting ownership before changing a store or recording its claim."""
                info = claim["metadata"]
                annotations = info.get("annotations", {})
                if (
                    (
                        not controlled(claim)
                        and any(
                            info.get("labels", {}).get(key) != value
                            for key, value in labels.items()
                        )
                    )
                    or info.get("labels", {}).get("app.kubernetes.io/managed-by", "Helm") != "Helm"
                    or any(
                        owner["uid"] not in owners
                        for owner in info.get("ownerReferences", [])
                    )
                    or any(
                        annotations.get(key, expected) != expected
                        for key, expected in (
                            ("meta.helm.sh/release-name", release.name),
                            ("meta.helm.sh/release-namespace", release.namespace),
                        )
                    )
                ):
                    raise DeploymentError(f"admission store PVC {release.namespace}/{info['name']} has another owner")

            claims = []
            for claim in kubectl.list_resources(("persistentvolumeclaims",), release.namespace):
                if not controlled(claim) and claim["metadata"]["name"] not in mounted:
                    continue
                validate_claim(claim)
                claims.append(claim)

            # Native StatefulSet ownership must settle before Helm removes the store.
            # Scaling retains claims; store deletion later lets GC reclaim only its PVCs.
            policy = {"whenDeleted": "Delete", "whenScaled": "Retain"}
            kubectl.run([
                "patch", "statefulset", name, "-n", release.namespace, "--type=merge", "--patch",
                json.dumps({
                    "metadata": {"resourceVersion": metadata["resourceVersion"]},
                    "spec": {"replicas": 0, "persistentVolumeClaimRetentionPolicy": policy},
                }),
            ])
            deadline = time.monotonic() + timeout_seconds(timeout)
            while True:
                current = kubectl.get_if_exists("statefulset", name, release.namespace)
                if (
                    current is None or current["metadata"]["uid"] != uid
                    or current["spec"].get("replicas") != 0
                    or current["spec"].get("persistentVolumeClaimRetentionPolicy") != policy
                ):
                    raise DeploymentError(f"admission store {release.namespace}/{name} changed during uninstall")
                stopped = not any(
                    controlled(pod) for pod in kubectl.list_resources(("pods",), release.namespace, label_selector=selector)
                )
                owned = True
                for claim in claims:
                    info = claim["metadata"]
                    current_claim = kubectl.get_if_exists("persistentvolumeclaim", info["name"], release.namespace)
                    if current_claim is not None and current_claim["metadata"]["uid"] == info["uid"] and not controlled(current_claim):
                        owned = False
                if stopped and owned:
                    break
                if time.monotonic() >= deadline:
                    raise DeploymentError(
                        f"admission store {release.namespace}/{name} did not finish stopping and assigning PVC ownership within {timeout}"
                    )
                time.sleep(1)
            # Persist the verified release identity so retries can finish after Helm removal.
            for claim in claims:
                info = claim["metadata"]
                current = kubectl.get_if_exists("persistentvolumeclaim", info["name"], release.namespace)
                if current is None or current["metadata"]["uid"] != info["uid"]:
                    continue
                if not controlled(current):
                    raise DeploymentError(f"PVC {release.namespace}/{info['name']} changed ownership during uninstall")
                validate_claim(current)
                kubectl.run([
                    "patch", "persistentvolumeclaim", info["name"], "-n", release.namespace,
                    "--type=merge", "--patch", json.dumps({
                        "metadata": {
                            "resourceVersion": current["metadata"]["resourceVersion"],
                            "labels": {**labels, "app.kubernetes.io/managed-by": "Helm"},
                            "annotations": {
                                "meta.helm.sh/release-name": release.name,
                                "meta.helm.sh/release-namespace": release.namespace,
                            },
                        },
                    }),
                ])
                claims_to_wait.append(info)
        return tuple(claims_to_wait)
