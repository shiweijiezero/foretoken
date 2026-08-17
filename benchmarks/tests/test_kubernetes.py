# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from benchmarks.arguments import CleanupCommand, RunCommand, parse_arguments
from benchmarks.kubernetes import KubernetesBenchmarkError, cleanup_managed, run_managed


class _FakeCluster:
    def __init__(self) -> None:
        self.namespaces: dict[str, dict[str, str]] = {}
        self.applied: dict[str, list[dict[str, Any]]] = {}
        self.deleted_namespaces: list[str] = []

    def run(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del input_text, check
        if args[:2] == ["get", "namespace"]:
            exists = args[2] in self.namespaces
            return subprocess.CompletedProcess(args, 0 if exists else 1, "", "")
        if args[:2] == ["delete", "namespace"]:
            namespace = args[2]
            self.namespaces.pop(namespace, None)
            self.deleted_namespaces.append(namespace)
        return subprocess.CompletedProcess(args, 0, "", "")

    def json(self, args: list[str]) -> dict[str, Any]:
        if args[:2] == ["get", "storageclass"]:
            return {
                "items": [
                    {
                        "metadata": {
                            "name": "default",
                            "annotations": {
                                "storageclass.kubernetes.io/is-default-class": "true"
                            },
                        }
                    }
                ]
            }
        if args[:2] == ["get", "service"]:
            return {"spec": {"ports": [{"name": "http", "port": 9090}]}}
        if args[:2] == ["get", "configmaps"]:
            return {
                "items": [
                    {
                        "data": {
                            "serving.json": json.dumps(
                                {
                                    "version": 7,
                                    "groups": [{"model": "org/model"}],
                                }
                            )
                        }
                    }
                ]
            }
        if args[:2] == ["get", "job"]:
            return {"status": {"conditions": [{"type": "Complete", "status": "True"}]}}
        if args[:2] == ["get", "pods"]:
            return {
                "items": [
                    {
                        "metadata": {"name": "benchmark-pod"},
                        "spec": {"nodeName": "worker-1"},
                        "status": {"containerStatuses": []},
                    }
                ]
            }
        if args[:2] == ["get", "namespace"]:
            return {"metadata": {"labels": self.namespaces[args[2]]}}
        raise AssertionError(f"unexpected kubectl JSON call: {args}")

    def apply(self, documents: list[dict[str, Any]]) -> None:
        for document in documents:
            kind = document["kind"]
            self.applied.setdefault(kind, []).append(document)
            if kind == "Namespace":
                self.namespaces[document["metadata"]["name"]] = document["metadata"][
                    "labels"
                ]

    def save_logs(self, namespace: str, job: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(f"{namespace}/{job} completed\n", encoding="utf-8")


def _deploy_file(path: Path) -> None:
    path.write_text(
        """\
apiVersion: v1
kind: Namespace
metadata:
  name: ignored
---
apiVersion: inference.foretoken.io/v1alpha1
kind: FrontendService
metadata:
  name: frontend
spec:
  replicas: 1
---
apiVersion: inference.foretoken.io/v1alpha1
kind: ModelService
metadata:
  name: model
spec:
  model: org/model
""",
        encoding="utf-8",
    )


def _managed_command(
    deploy: Path, output_dir: Path, *, keep: bool = False
) -> RunCommand:
    args = [
        "bench",
        "run",
        "--deploy",
        str(deploy),
        "--benchmark-image",
        "registry.example/benchmark:test",
        "--output-dir",
        str(output_dir),
    ]
    if keep:
        args.append("--keep")
    command = parse_arguments(args)
    assert isinstance(command, RunCommand)
    return command


def test_managed_run_collects_results_and_enforces_resource_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deploy = tmp_path / "deploy.yaml"
    _deploy_file(deploy)
    cluster = _FakeCluster()
    monkeypatch.setattr("benchmarks.kubernetes.Kubectl", lambda: cluster)

    def copy_results(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        assert command[:2] == ["kubectl", "cp"]
        assert command[-1] == "--container=transfer"
        destination = Path(command[-2])
        destination.mkdir(parents=True, exist_ok=True)
        for filename, data in (
            ("manifest.json", {"resources_owned": False, "phase": "completed"}),
            ("config.json", {"target": {"api_key": ""}}),
            ("raw-output.json", [{"success": True}]),
            ("metrics.json", {"success_num": 1, "request_num": 1}),
        ):
            (destination / filename).write_text(json.dumps(data), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("benchmarks.kubernetes.subprocess.run", copy_results)

    run_managed(_managed_command(deploy, tmp_path), "managed-success")
    manifest = json.loads((tmp_path / "managed-success" / "manifest.json").read_text())
    assert manifest["resources_owned"] is True
    assert manifest["artifacts_collected"] is True
    assert manifest["phase"] == "cleaned"
    assert "managed-success" not in cluster.namespaces

    job = cluster.applied["Job"][0]
    pod_spec = job["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    assert (
        "http://frontend.managed-success.svc.cluster.local:9090/v1" in container["args"]
    )
    readiness = pod_spec["initContainers"][0]
    readiness_env = {item["name"]: item["value"] for item in readiness["env"]}
    assert readiness_env["FORETOKEN_FRONTEND_GENERATION"] == "7"
    assert readiness_env["FORETOKEN_FRONTEND_STATUS_URL"].endswith("/statusz")
    transfer = cluster.applied["Pod"][0]
    assert transfer["spec"]["nodeName"] == "worker-1"
    assert transfer["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"]

    run_managed(_managed_command(deploy, tmp_path, keep=True), "managed-retained")
    assert "managed-retained" in cluster.namespaces
    cleanup = CleanupCommand(run_id="managed-retained", output_dir=str(tmp_path))

    cluster.namespaces["managed-retained"]["app.kubernetes.io/managed-by"] = "other"
    with pytest.raises(KubernetesBenchmarkError, match="not owned"):
        cleanup_managed(cleanup)
    assert "managed-retained" in cluster.namespaces

    cluster.namespaces["managed-retained"]["app.kubernetes.io/managed-by"] = (
        "foretoken-bench"
    )
    cleanup_managed(cleanup)
    retained_manifest = json.loads(
        (tmp_path / "managed-retained" / "manifest.json").read_text()
    )
    assert retained_manifest["phase"] == "cleaned"
    assert "managed-retained" not in cluster.namespaces
