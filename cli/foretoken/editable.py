# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Prepare source-installed runtime updates without rebuilding their environment images."""

from __future__ import annotations

import copy
import filecmp
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import yaml

from foretoken.arguments import InstallCommand
from foretoken.kubernetes import Kubectl, timeout_seconds
from foretoken.manifest import DeploymentError, ForetokenDeployment, parse_deployment

SOURCE_REVISION = "inference.foretoken.io/source-revision"
_INSTALL_SOURCE = "foretoken.io/install-source"
_COMPONENTS = {"ModelService": "model-server", "FrontendService": "frontend"}


def _source_home() -> Path:
    return (
        Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        / "foretoken"
        / "source"
    )


def _context_identity(kubectl: Kubectl) -> dict[str, str]:
    """Read connection identity locally without adding API permissions to release deploys."""
    config = json.loads(
        kubectl.run(["config", "view", "--minify", "-o", "json"]).stdout
    )
    return {"server": config["clusters"][0]["cluster"]["server"]}


def _state_directory(kubectl: Kubectl) -> Path:
    """Keep workstation paths local and bind them to the actual cluster identity."""
    cluster = kubectl.get("namespace", "kube-system")["metadata"]["uid"]
    return _source_home() / cluster


def _has_server_binding(kubectl: Kubectl) -> bool:
    """Prefilter local bindings without requiring cluster access for release deployments."""
    bindings = list(_source_home().glob("*/install.json"))
    if not bindings:
        return False
    server = _context_identity(kubectl)["server"]
    for path in bindings:
        try:
            state = json.loads(path.read_text())
        except FileNotFoundError:
            # An uninstall may retire a binding before this operation acquires its lock.
            continue
        if state["context"]["server"] == server:
            return True
    return False


@contextmanager
def source_operation(kubectl: Kubectl, *, installing: bool = False) -> Iterator[None]:
    """Serialize CLI source operations by cluster, including artifact readers and cleanup.

    The CLI entry point holds this lock through install, deploy, or uninstall. Internal
    image rebuilds call the platform lifecycle directly within the same operation.
    """
    if not installing and not _has_server_binding(kubectl):
        yield
        return
    import fcntl

    directory = _state_directory(kubectl)
    locks = _source_home() / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    with (locks / directory.name).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _inputs(root: Path) -> dict[str, Path]:
    """Select build inputs using Git's tracked and non-ignored source files."""
    command = [
        "git",
        "-C",
        str(root),
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
    ]
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode:
        raise DeploymentError(result.stderr.decode().strip())
    files = {}
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        name = os.fsdecode(raw)
        path = root / name
        if not name.startswith(
            ("data-plane/", "control-plane/", "deploy/")
        ) and name not in {"Makefile", ".dockerignore", ".gitmodules"}:
            continue
        if path.is_file() and path.suffix not in {".md", ".png", ".svg"}:
            files[name] = path
    # Rust dependencies use the pinned submodule. Its own ignore rules exclude build caches.
    upstream = root / "data-plane/third_party/vllm"
    if (upstream / ".git").exists():
        result = subprocess.run(
            [
                "git",
                "-C",
                str(upstream),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
                "rust",
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise DeploymentError(result.stderr.decode().strip())
        for raw in result.stdout.split(b"\0"):
            if raw:
                name = "data-plane/third_party/vllm/" + os.fsdecode(raw)
                if (root / name).is_file():
                    files[name] = root / name
    return files


def _snapshot(files: dict[str, Path], destination: Path) -> None:
    """Save exact input bytes so timestamps and unchanged Git commits cannot hide edits."""
    destination.mkdir(parents=True)
    for name, source in files.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _write_json(path: Path, value: object) -> None:
    """Replace one local source state only after its complete payload has been written."""
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def capture_build_inputs(root: Path) -> Iterator[Path]:
    """Own candidate image inputs until record_install moves them into committed state."""
    destination = _source_home() / "builds" / ("inputs-" + uuid.uuid4().hex)
    try:
        _snapshot(_inputs(root), destination)
        yield destination
    finally:
        if destination.exists():
            shutil.rmtree(destination)


@contextmanager
def _local_candidates(directory: Path) -> Iterator[None]:
    """Retire unreferenced local artifacts after a state commit or failed preparation.

    The cluster operation lock keeps other CLI readers out during retirement. Running
    workloads consume their published cache copies rather than these local candidates.
    """
    try:
        yield
    finally:
        path = directory / "install.json"
        state = json.loads(path.read_text()) if path.is_file() else {}
        for snapshot in directory.glob("inputs-*"):
            if snapshot.name != state.get("inputs"):
                shutil.rmtree(snapshot)
        retained = {
            Path(bundle["path"]).parent
            for bundle in state.get("bundles", {}).values()
        }
        for bundle in (directory / "bundles").glob("*"):
            if bundle not in retained:
                shutil.rmtree(bundle)


def forget_install(kubectl: Kubectl) -> None:
    """Remove this cluster's workstation binding after platform uninstall, retaining remote data."""
    if _has_server_binding(kubectl):
        directory = _state_directory(kubectl)
        if directory.exists():
            shutil.rmtree(directory)


def validate_build_inputs(root: Path, snapshot: Path) -> None:
    """Reject a build whose checkout changed before its image or bundle was selected."""
    current = _inputs(root)
    previous = {
        str(path.relative_to(snapshot))
        for path in snapshot.rglob("*")
        if path.is_file()
    }
    if previous != current.keys() or any(
        not filecmp.cmp(snapshot / name, path, shallow=False)
        or (snapshot / name).stat().st_mode != path.stat().st_mode
        for name, path in current.items()
    ):
        raise DeploymentError(
            "source changed while preparing artifacts; rerun the source operation"
        )


def record_install(
    kubectl: Kubectl, command: InstallCommand, base_image: str | None, snapshot: Path
) -> None:
    """Associate a successful editable installation with its checkout on this workstation."""
    if command.editable is None:
        return
    root = Path(command.editable).expanduser().resolve()
    directory = _state_directory(kubectl)
    directory.mkdir(parents=True, exist_ok=True)
    settings = asdict(command)
    settings.update(editable=str(root), values=[])
    platforms = kubectl.list_all_resources(
        ("deployment.apps",),
        label_selector="app.kubernetes.io/name=foretoken-control-plane",
    )
    managed = [
        p
        for p in platforms
        if p["metadata"].get("annotations", {}).get(_INSTALL_SOURCE) == "source"
    ]
    if len(managed) != 1:
        raise DeploymentError("expected one source-installed Foretoken platform")
    arguments = managed[0]["spec"]["template"]["spec"]["containers"][0]["args"]
    options = dict(
        arg[2:].split("=", 1)
        for arg in arguments
        if arg.startswith("--") and "=" in arg
    )
    # A full image build already includes source changes. Retire overlays for every service,
    # not only the deployment which happened to trigger the environment update.
    for service in kubectl.list_all_resources(("modelservice", "frontendservice")):
        metadata = service["metadata"]
        if SOURCE_REVISION in metadata.get("annotations", {}):
            kubectl.run(
                [
                    "patch",
                    service["kind"],
                    metadata["name"],
                    "-n",
                    metadata["namespace"],
                    "--type=json",
                    "-p",
                    json.dumps(
                        [
                            {
                                "op": "test",
                                "path": "/metadata/resourceVersion",
                                "value": metadata["resourceVersion"],
                            },
                            {
                                "op": "remove",
                                "path": "/metadata/annotations/inference.foretoken.io~1source-revision",
                            },
                        ]
                    ),
                ]
            )
    with _local_candidates(directory):
        destination = directory / snapshot.name
        snapshot.rename(destination)
        _write_json(
            directory / "install.json",
            {
                "root": str(root),
                "context": _context_identity(kubectl),
                "inputs": destination.name,
                "platform_uid": managed[0]["metadata"]["uid"],
                "base_image": base_image,
                "runtime": {
                    "image": options["frontend-image"],
                    "model_image": options["inference-engine-image"],
                    "omni_image": options.get("omni-inference-engine-image", ""),
                    "nsight_image": options.get("nsight-image", ""),
                    "mount": options["cache-mount-path"],
                    "claim": options.get("cache-claim", ""),
                    "pull_secrets": [
                        arg.split("=", 1)[1]
                        for arg in arguments
                        if arg.startswith("--workload-image-pull-secret=")
                    ],
                },
                "command": settings,
                "bundles": {},
            },
        )


class EditableDeployment:
    """Own local source comparison, candidate preparation and target-service activation."""

    def __init__(
        self, kubectl: Kubectl, directory: Path, state: dict[str, Any]
    ) -> None:
        self.kubectl = kubectl
        self.directory = directory
        self.state = state
        self.root = Path(state["root"])
        self.selected: dict[tuple[str, str, str], str] = {}

    @classmethod
    def discover(cls, kubectl: Kubectl) -> EditableDeployment | None:
        """Enable source updates only for a platform installed explicitly from source."""
        if not _has_server_binding(kubectl):
            return None
        platforms = kubectl.list_all_resources(
            ("deployment.apps",),
            label_selector="app.kubernetes.io/name=foretoken-control-plane",
        )
        source = [
            p
            for p in platforms
            if p["metadata"].get("annotations", {}).get(_INSTALL_SOURCE) == "source"
        ]
        if not source:
            return None
        directory = _state_directory(kubectl)
        path = directory / "install.json"
        if not path.is_file():
            raise DeploymentError(
                "source checkout is not associated with this workstation; run foretoken install -e PATH"
            )
        state = json.loads(path.read_text())
        if (
            len(source) != 1
            or state.get("platform_uid") != source[0]["metadata"]["uid"]
        ):
            raise DeploymentError(
                "source installation changed; associate its checkout with foretoken install -e PATH"
            )
        if not Path(state["root"]).is_dir():
            raise DeploymentError(
                f"source checkout is unavailable: {state['root']}; run foretoken install -e PATH"
            )
        return cls(kubectl, directory, state)

    def prepare(self, timeout: str) -> None:
        """Prepare changed runtime artifacts and retire candidates not retained by committed state."""
        with _local_candidates(self.directory):
            self._prepare(timeout)

    def _prepare(self, timeout: str) -> None:
        """Compile changed components or reuse installation for build-environment changes."""
        current = _inputs(self.root)
        old = self.directory / self.state["inputs"]
        previous = {str(p.relative_to(old)) for p in old.rglob("*") if p.is_file()}
        changed = {
            name
            for name in previous | current.keys()
            if name not in previous
            or name not in current
            or not filecmp.cmp(old / name, current[name], shallow=False)
            or (old / name).stat().st_mode != current[name].stat().st_mode
        }
        if not changed:
            print("Source code unchanged; reusing runtime artifacts", flush=True)
            return
        components: set[str] = set()
        compile_components: set[str] = set()
        rebuild = False
        for name in changed:
            if name == "data-plane/artifacts/src/source.rs":
                # The image's bootstrap runs before the candidate executable can take over.
                rebuild = True
            elif name.startswith("data-plane/model-server/python/") and name.endswith(
                ".py"
            ):
                components.add("model-server")
            elif name.startswith("data-plane/") and name.endswith(".rs"):
                targets = (
                    {"frontend"}
                    if name.startswith("data-plane/frontend/")
                    else {"model-server"}
                    if name.startswith("data-plane/model-server/")
                    else {"frontend", "model-server"}
                )
                components.update(targets)
                compile_components.update(targets)
            else:
                rebuild = True
        if rebuild:
            print(
                "Build environment or platform sources changed; updating source installation",
                flush=True,
            )
            self._rebuild(timeout)
            return
        # Build from one staged view; the saved snapshot is also the comparison input next time.
        revision = str(uuid.uuid4())
        snapshot = self.directory / ("inputs-" + revision)
        _snapshot(current, snapshot)
        bundles = dict(self.state["bundles"])
        for component in sorted(components):
            revision = str(uuid.uuid4())
            destination = self.directory / "bundles" / revision / component
            destination.mkdir(parents=True)
            previous_bundle = bundles.get(component)
            if previous_bundle:
                shutil.copytree(
                    Path(previous_bundle["path"]), destination, dirs_exist_ok=True
                )
            if component == "model-server":
                python = destination / "python"
                if python.exists():
                    shutil.rmtree(python)
                shutil.copytree(snapshot / "data-plane/model-server/python", python)
            if component in compile_components:
                print(
                    f"Compiling {component} changes with the existing build cache",
                    flush=True,
                )
                self._compile(component, destination)
            _write_json(
                destination / "complete.json",
                {
                    "revision": revision,
                    "component": component,
                    "executable": f"foretoken-{component}"
                    if (destination / "bin" / f"foretoken-{component}").is_file()
                    else None,
                },
            )
            bundles[component] = {"revision": revision, "path": str(destination)}
        # A build must not mark concurrently edited files as already deployed.
        validate_build_inputs(self.root, snapshot)
        self.state.update(inputs=snapshot.name, bundles=bundles)
        _write_json(self.directory / "install.json", self.state)

    def _rebuild(self, timeout: str) -> None:
        """Let platform installation own environment images and retain installed values."""
        from foretoken.platform import PlatformLifecycle

        settings = dict(self.state["command"])
        settings.update(values=(), timeout=timeout)
        command = InstallCommand(**settings)
        PlatformLifecycle(command.oci_registry).install(
            command, source_base_image=self.state.get("base_image")
        )
        self.state = json.loads((self.directory / "install.json").read_text())
        self.selected.clear()

    def _compile(self, component: str, destination: Path) -> None:
        """Export cached Linux build output without creating or distributing a runtime image."""
        from foretoken.network_sources import select_source_build_sources

        environment = os.environ.copy()
        if self.state["command"].get("oci_registry"):
            environment["FORETOKEN_OCI_REGISTRY"] = self.state["command"][
                "oci_registry"
            ]
        selected, _, _ = select_source_build_sources(environment)
        environment.update(selected)
        command = [str(self.root / "deploy/dev-build"), component, str(destination)]
        result = subprocess.run(command, cwd=self.root, env=environment, check=False)
        if result.returncode:
            raise DeploymentError(
                f"{component} artifact build failed with exit code {result.returncode}"
            )

    def apply(
        self, deployment: ForetokenDeployment, timeout: str
    ) -> ForetokenDeployment:
        """Publish complete bundles to target caches and project their revisions into service intent."""
        objects = copy.deepcopy(deployment.objects)
        pending: dict[str, dict[str, dict[str, str]]] = {}
        for obj in objects:
            component = _COMPONENTS.get(obj.get("kind"))
            if component is None:
                continue
            metadata = obj["metadata"]
            annotations = metadata.setdefault("annotations", {})
            annotations.pop(SOURCE_REVISION, None)
            bundle = self.state["bundles"].get(component)
            namespace = metadata.get("namespace") or deployment.namespace or "default"
            self.selected[(obj["kind"], namespace, metadata["name"])] = (
                bundle["revision"] if bundle else ""
            )
            if bundle is None:
                continue
            if component == "model-server" and obj["spec"].get("backend") != "vllm":
                raise DeploymentError(
                    "source runtime artifacts currently require the vLLM backend"
                )
            current = self.kubectl.get_if_exists(
                obj["kind"], metadata["name"], namespace
            )
            active = (
                (current or {})
                .get("metadata", {})
                .get("annotations", {})
                .get(SOURCE_REVISION)
            )
            if active != bundle["revision"]:
                pending.setdefault(namespace, {})[component] = bundle
            annotations[SOURCE_REVISION] = bundle["revision"]
        if pending:
            # Storage can be prepared without requiring the previous engine to start.
            from foretoken.storage import DirectoryVolumes

            storage = tuple(
                o
                for o in deployment.objects
                if o.get("kind") in {"Namespace", "RuntimeCache"}
            )
            if storage:
                DirectoryVolumes(self.kubectl).apply(
                    replace(
                        deployment,
                        objects=storage,
                        rendered=yaml.safe_dump_all(storage, sort_keys=False),
                    ),
                    timeout,
                )
            for namespace, bundles in pending.items():
                if not self._publish(namespace, bundles, timeout):
                    print(
                        f"{namespace} source storage is not ready or unavailable; updating images instead",
                        flush=True,
                    )
                    self._rebuild(timeout)
                    return self.apply(deployment, timeout)
        return parse_deployment(
            deployment.path, yaml.safe_dump_all(objects, sort_keys=False)
        )

    def _publish(
        self, namespace: str, bundles: dict[str, dict[str, str]], timeout: str
    ) -> bool:
        """Use one CPU publisher to populate the cache independently of inference startup."""
        runtime = self.state["runtime"]
        claim = runtime["claim"]
        if not claim:
            caches = self.kubectl.list_resources(("runtimecache",), namespace)
            if not caches:
                return False
            if len(caches) != 1:
                raise DeploymentError(
                    f"namespace {namespace} has multiple RuntimeCaches"
                )
            name = caches[0]["metadata"]["name"]
            self.kubectl.run(
                [
                    "wait",
                    f"runtimecache/{name}",
                    "-n",
                    namespace,
                    "--for=jsonpath={.status.claimName}",
                    f"--timeout={timeout}",
                ]
            )
            claim = self.kubectl.get("runtimecache", name, namespace)["status"][
                "claimName"
            ]
        pvc = self.kubectl.get("pvc", claim, namespace)
        # Model preparation owns first placement of single-node writable storage on GPU nodes.
        if (
            pvc.get("status", {}).get("phase") != "Bound"
            and "ReadWriteMany" not in pvc["spec"]["accessModes"]
        ):
            return False
        name = "foretoken-source-" + uuid.uuid4().hex[:12]
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": {"inference.foretoken.io/source-publisher": "true"},
            },
            "spec": {
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "activeDeadlineSeconds": int(timeout_seconds(timeout)),
                "imagePullSecrets": [
                    {"name": value} for value in runtime["pull_secrets"]
                ],
                "volumes": [
                    {"name": "source", "persistentVolumeClaim": {"claimName": claim}}
                ],
                "containers": [
                    {
                        "name": "publisher",
                        "image": runtime["model_image"],
                        "imagePullPolicy": "IfNotPresent",
                        "env": [{"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"}],
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                        },
                        "command": ["sleep", str(int(timeout_seconds(timeout)))],
                        "volumeMounts": [
                            {"name": "source", "mountPath": runtime["mount"]}
                        ],
                    }
                ],
            },
        }
        # Keep the configured engine image's user for every publication. A frontend-only
        # update must not switch the owner of the shared source directory to UID 65532.
        mounts = [
            existing
            for existing in self.kubectl.list_resources(("pods",), namespace)
            if existing["spec"].get("nodeName")
            and any(
                v.get("persistentVolumeClaim", {}).get("claimName") == claim
                for v in existing["spec"].get("volumes", [])
            )
        ]
        if mounts:
            existing = min(
                mounts,
                key=lambda p: (
                    p.get("status", {}).get("phase") != "Running",
                    bool(p["metadata"].get("deletionTimestamp")),
                ),
            )
            pod["spec"]["nodeName"] = existing["spec"]["nodeName"]
        self.kubectl.run(["create", "-f", "-"], input_text=yaml.safe_dump(pod))
        try:
            self.kubectl.run(
                [
                    "wait",
                    "pod/" + name,
                    "-n",
                    namespace,
                    "--for=condition=Ready",
                    f"--timeout={timeout}",
                ]
            )
            for bundle in bundles.values():
                self._upload(
                    bundle, namespace, name, "publisher", runtime["mount"], timeout
                )
        finally:
            self.kubectl.run(
                [
                    "delete",
                    "pod",
                    name,
                    "-n",
                    namespace,
                    "--ignore-not-found",
                    "--wait=true",
                    f"--timeout={timeout}",
                ]
            )
        return True

    def _ready_containers(
        self, namespace: str, component: str, service: str, revision: str
    ) -> list[tuple[str, str, str]]:
        """Select the committed source or image cohort, not an earlier ready generation."""
        containers = []
        selected = {}
        seen_pools = set()
        if component == "model-server":
            current = self.kubectl.get("modelservice", service, namespace)
            selected = {
                p["poolUID"]: p["revision"]
                for p in current.get("status", {}).get("servingPoolRevisions", [])
            }
        for pod in self.kubectl.list_resources(("pods",), namespace):
            metadata = pod["metadata"]
            if (
                metadata.get("deletionTimestamp")
                or metadata.get("annotations", {}).get(SOURCE_REVISION, "") != revision
            ):
                continue
            if not any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in pod.get("status", {}).get("conditions", [])
            ):
                continue
            labels = metadata.get("labels", {})
            expected_image = self.state["runtime"]["image"]
            if component == "frontend":
                if labels.get("inference.foretoken.io/frontend-service") != service:
                    continue
            else:
                group_name = labels.get("inference.foretoken.io/model-group")
                if not group_name:
                    continue
                group = self.kubectl.get_if_exists("modelgroup", group_name, namespace)
                if group is None:
                    continue
                pool = self.kubectl.get_if_exists(
                    "modelpool", group["spec"]["modelPoolRef"]["name"], namespace
                )
                if (
                    pool is None
                    or group["spec"]["modelPoolRef"]["uid"] != pool["metadata"]["uid"]
                ):
                    continue
                runtime = group["spec"]["runtime"]
                if (
                    pool["spec"]["modelServiceRef"]["name"] != service
                    or selected.get(pool["metadata"]["uid"])
                    != group["spec"]["revision"]
                    or runtime.get("sourceRevision", "") != revision
                ):
                    continue
                image_key = (
                    "omni_image"
                    if runtime["backend"] == "vllm-omni"
                    else "nsight_image"
                    if runtime.get("profiling", {}).get("engine") == "nsight"
                    else "model_image"
                )
                expected_image = self.state["runtime"][image_key]
                if runtime["image"] != expected_image:
                    continue
                seen_pools.add(pool["metadata"]["uid"])
            for container in pod["spec"]["containers"]:
                if (
                    container["name"] == component
                    and container["image"] == expected_image
                ):
                    directory = next(
                        (
                            e.get("value", "")
                            for e in container.get("env", [])
                            if e["name"] == "FORETOKEN_SOURCE_DIRECTORY"
                        ),
                        "",
                    )
                    containers.append((metadata["name"], component, directory))
        if component == "model-server" and seen_pools != set(selected):
            return []
        return containers

    def _upload(
        self,
        bundle: dict[str, str],
        namespace: str,
        pod: str,
        container: str,
        mount: str,
        timeout: str,
    ) -> None:
        """Stage immutable files before exposing a source revision to any new runtime."""
        revision = bundle["revision"]
        destination = f"{mount}/source/{revision}"
        command = self.kubectl.command(
            [
                "exec",
                "-i",
                "-n",
                namespace,
                pod,
                "-c",
                container,
                "--",
                "sh",
                "-c",
                'set -eu; mkdir -p -m 2775 "${1%/*}"; if test -f "$1/complete.json"; then cat >/dev/null; exit 0; fi; stage=$(mktemp -d "$1.staging.XXXXXX"); tar --no-same-owner -xf - -C "$stage"; test -f "$stage/complete.json"; chmod -R a+rX "$stage"; mv -T "$stage" "$1"',
                "source-upload",
                destination,
            ]
        )
        with tempfile.TemporaryFile() as archive:
            with tarfile.open(fileobj=archive, mode="w") as tar:
                for path in sorted(Path(bundle["path"]).rglob("*")):
                    if path.is_file():
                        tar.add(
                            path,
                            arcname=str(path.relative_to(bundle["path"])),
                            recursive=False,
                        )
            archive.seek(0)
            result = subprocess.run(command, stdin=archive, check=False)
        if result.returncode:
            raise DeploymentError(f"source upload failed for {namespace}/{pod}")
        print(f"Source {revision}: prepared {namespace}/{pod}", flush=True)

    def verify(
        self, timeout: str, *, observe: Callable[[], None] | None = None
    ) -> None:
        """Wait for source-aware readiness and check each selected process after replacement."""
        deadline = time.monotonic() + timeout_seconds(timeout)
        for (kind, namespace, service), revision in self.selected.items():
            component = _COMPONENTS[kind]
            while True:
                if observe is not None:
                    observe()
                writers = self._ready_containers(
                    namespace, component, service, revision
                )
                if writers:
                    break
                if time.monotonic() >= deadline:
                    raise DeploymentError(
                        f"timed out waiting for {namespace}/{service} to activate source {revision}"
                    )
                time.sleep(2)
            if component == "frontend":
                from foretoken.manifest import ResourceRef

                self.kubectl.rollout_status(
                    ResourceRef("Deployment", service, namespace), timeout
                )
                writers = self._ready_containers(
                    namespace, component, service, revision
                )
            for pod, container, directory in writers:
                expected = (
                    f"FORETOKEN_ACTIVE_SOURCE_DIRECTORY={directory}" if revision else ""
                )
                output = self.kubectl.run(
                    [
                        "exec",
                        "-n",
                        namespace,
                        pod,
                        "-c",
                        container,
                        "--",
                        "sh",
                        "-c",
                        'grep -z "^FORETOKEN_ACTIVE_SOURCE_DIRECTORY=" /proc/1/environ; status=$?; test "$status" -le 1',
                    ]
                ).stdout.strip("\x00\n")
                if output != expected:
                    raise DeploymentError(
                        f"{namespace}/{pod} did not activate source {revision}"
                    )
