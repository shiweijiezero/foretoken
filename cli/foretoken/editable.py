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
from foretoken.cluster_build import (
    ClusterBuilder,
    registry_credentials,
    remove_build_pods,
)
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
    bindings = [
        *(_source_home().glob("*/install.json")),
        *(_source_home().glob("*/build.json")),
    ]
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
def source_operation(
    kubectl: Kubectl, timeout: str, *, installing: bool = False
) -> Iterator[None]:
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
            bindings = set()
            for name in ("install.json", "build.json"):
                path = directory / name
                if path.is_file():
                    binding = (
                        json.loads(path.read_text()).get("build", {}).get("binding")
                    )
                    if binding:
                        bindings.add(binding)
            for binding in bindings:
                remove_build_pods(kubectl, timeout, binding=binding)
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def engine_source_roots(sources: tuple[str, ...]) -> dict[str, str]:
    """Resolve explicitly selected runtime checkouts, independently of Rust's pinned dependency."""
    roots = {}
    for source in sources:
        name, separator, value = source.partition("=")
        if not separator:
            name, value = "vllm", source
        if name not in {"vllm", "vllm-metax"} or name in roots:
            raise DeploymentError(
                "--engine-source accepts one vllm checkout and one optional vllm-metax checkout"
            )
        path = Path(value).expanduser().resolve()
        package = path / name.replace("-", "_")
        if not package.is_dir() or not (path / "setup.py").is_file():
            raise DeploymentError(f"engine source is not a {name} checkout: {path}")
        roots[name] = str(path)
    if roots and "vllm" not in roots:
        raise DeploymentError(
            "--engine-source vllm-metax=PATH also requires the corresponding --engine-source vllm=PATH"
        )
    return roots


def _inputs(root: Path, engines: dict[str, str] | None = None) -> dict[str, Path]:
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
        ) and name not in {"Makefile", "LICENSE", ".dockerignore", ".gitmodules"}:
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
    mooncake = root / "third_party/mooncake"
    if (mooncake / ".git").exists():
        result = subprocess.run(
            ["git", "-C", str(mooncake), "ls-files", "--recurse-submodules", "-z"],
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise DeploymentError(result.stderr.decode().strip())
        for raw in result.stdout.split(b"\0"):
            if raw:
                name = "third_party/mooncake/" + os.fsdecode(raw)
                if (root / name).is_file():
                    files[name] = root / name
    for engine, checkout in (engines or {}).items():
        result = subprocess.run(
            [
                "git",
                "-C",
                checkout,
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise DeploymentError(result.stderr.decode().strip())
        for raw in result.stdout.split(b"\0"):
            if not raw:
                continue
            name = os.fsdecode(raw)
            path = Path(checkout) / name
            if path.is_file():
                files[f"engine/{engine}/{name}"] = path
    return files


def _snapshot(
    files: dict[str, Path], destination: Path, engines: dict[str, str] | None = None
) -> None:
    """Save exact input bytes so timestamps and unchanged Git commits cannot hide edits."""
    destination.mkdir(parents=True)
    for name, source in files.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    if engines:
        manifest = {}
        deleted = {}
        for engine, checkout in engines.items():
            prefix = f"engine/{engine}/"
            manifest[engine] = {
                "files": sorted(
                    name.removeprefix(prefix)
                    for name in files
                    if name.startswith(prefix)
                )
            }
            result = subprocess.run(
                ["git", "-C", checkout, "ls-files", "--deleted", "-z"],
                capture_output=True,
                check=True,
            )
            deleted[engine] = [
                os.fsdecode(name) for name in result.stdout.split(b"\0") if name
            ]
        (destination / "engine").mkdir(exist_ok=True)
        _write_json(destination / "engine/manifest.json", manifest)
        _write_json(destination / "engine/deleted.json", deleted)


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
def capture_build_inputs(
    root: Path, engines: dict[str, str] | None = None
) -> Iterator[Path]:
    """Own candidate image inputs until record_install moves them into committed state."""
    destination = _source_home() / "builds" / ("inputs-" + uuid.uuid4().hex)
    try:
        _snapshot(_inputs(root, engines), destination, engines)
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
        if (directory / "bundles").is_dir():
            shutil.rmtree(directory / "bundles")


def has_source_state(kubectl: Kubectl) -> bool:
    """Identify installed or interrupted source builds belonging to this actual cluster."""
    if not _has_server_binding(kubectl):
        return False
    directory = _state_directory(kubectl)
    return any((directory / name).is_file() for name in ("install.json", "build.json"))


def forget_install(kubectl: Kubectl) -> None:
    """Remove this cluster's workstation binding after platform uninstall, retaining remote data."""
    if _has_server_binding(kubectl):
        directory = _state_directory(kubectl)
        if directory.exists():
            shutil.rmtree(directory)


def validate_build_inputs(
    root: Path, snapshot: Path, engines: dict[str, str] | None = None
) -> None:
    """Reject a build whose checkout changed before its image or bundle was selected."""
    current = _inputs(root, engines)
    previous = {
        str(path.relative_to(snapshot))
        for path in snapshot.rglob("*")
        if path.is_file()
        and str(path.relative_to(snapshot))
        not in {"engine/manifest.json", "engine/deleted.json"}
    }
    if previous != current.keys() or any(
        not filecmp.cmp(snapshot / name, path, shallow=False)
        or (snapshot / name).stat().st_mode != path.stat().st_mode
        for name, path in current.items()
    ):
        raise DeploymentError(
            "source changed while preparing artifacts; rerun the source operation"
        )


def snapshot_versions(
    snapshot: Path, previous: Path | None, versions: dict[str, str]
) -> dict[str, str]:
    """Assign a new revision only to changed input paths for cluster-side delta transfer."""
    revision = snapshot.name.removeprefix("inputs-")
    result = {}
    for path in snapshot.rglob("*"):
        if not path.is_file():
            continue
        name = str(path.relative_to(snapshot))
        old = previous / name if previous else None
        unchanged = (
            name in versions
            and old is not None
            and old.is_file()
            and old.stat().st_mode == path.stat().st_mode
            and filecmp.cmp(old, path, shallow=False)
        )
        result[name] = versions[name] if unchanged else revision
    return result


def _runtime_settings(platform: dict[str, Any]) -> dict[str, Any]:
    """Read the controller-owned runtime environment used to bind and validate source builds."""
    arguments = platform["spec"]["template"]["spec"]["containers"][0]["args"]
    options = dict(
        arg[2:].split("=", 1)
        for arg in arguments
        if arg.startswith("--") and "=" in arg
    )
    return {
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
    }


def record_install(
    kubectl: Kubectl,
    command: InstallCommand,
    base_image: str | None,
    snapshot: Path,
    *,
    build_state: dict[str, Any] | None = None,
) -> None:
    """Associate a successful editable installation with its checkout on this workstation."""
    if command.editable is None:
        return
    root = Path(command.editable).expanduser().resolve()
    directory = _state_directory(kubectl)
    directory.mkdir(parents=True, exist_ok=True)
    settings = asdict(command)
    engines = engine_source_roots(command.engine_sources)
    settings.update(
        editable=str(root),
        values=[],
        engine_sources=[f"{name}={path}" for name, path in engines.items()],
    )
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
                "engines": engines,
                "build": build_state or {},
                "context": _context_identity(kubectl),
                "inputs": destination.name,
                "platform_uid": managed[0]["metadata"]["uid"],
                "base_image": base_image,
                "runtime": _runtime_settings(managed[0]),
                "command": settings,
                "bundles": {},
            },
        )
        (directory / "build.json").unlink(missing_ok=True)


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
                "source checkout is not associated with this workstation; run `foretoken install -e .` from that checkout's root"
            )
        state = json.loads(path.read_text())
        if (
            len(source) != 1
            or state.get("platform_uid") != source[0]["metadata"]["uid"]
            or state["runtime"] != _runtime_settings(source[0])
        ):
            raise DeploymentError(
                "source installation changed; run `foretoken install -e .` from the intended checkout's root"
            )
        if not Path(state["root"]).is_dir():
            raise DeploymentError(
                f"source checkout is unavailable: {state['root']}; run `foretoken install -e .` from that checkout's root"
            )
        return cls(kubectl, directory, state)

    def prepare(self, timeout: str) -> None:
        """Prepare changed runtime artifacts and retire candidates not retained by committed state."""
        with _local_candidates(self.directory):
            self._prepare(timeout)

    def _prepare(self, timeout: str) -> None:
        """Capture a runtime update or reuse installation for build-environment changes."""
        from foretoken.source import pinned_rust_revision

        if not self.state.get("build") or self.state["build"]["arguments"].get(
            "VLLM_REVISION"
        ) != pinned_rust_revision(self.root):
            self._rebuild(timeout)
            return
        engines = self.state.get("engines", {})
        current = _inputs(self.root, engines)
        old = self.directory / self.state["inputs"]
        previous = {
            str(p.relative_to(old))
            for p in old.rglob("*")
            if p.is_file()
            and str(p.relative_to(old))
            not in {"engine/manifest.json", "engine/deleted.json"}
        }
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
            if name.startswith("engine/"):
                if Path(name).suffix in {".md", ".png", ".svg"}:
                    continue
                parts = Path(name).parts
                native_source = (
                    "vllm-metax"
                    if self.state["build"]["backend"] == "metax"
                    else "vllm"
                )
                if parts[1] == native_source and (
                    parts[2] in {"csrc", "cmake", "CMakeLists.txt"}
                    or Path(name).suffix
                    in {".cu", ".cuh", ".cpp", ".cc", ".c", ".h", ".hpp", ".cmake"}
                ):
                    self.state["build"]["engine_native"] = True
                if len(parts) > 2 and (
                    parts[2]
                    in {
                        "requirements",
                        "pyproject.toml",
                        "setup.py",
                        "setup.cfg",
                        "Dockerfile",
                    }
                ):
                    rebuild = True
                else:
                    components.add("model-server")
            elif name == "data-plane/artifacts/src/source.rs":
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
        snapshot = self.directory / ("inputs-" + str(uuid.uuid4()))
        _snapshot(current, snapshot, engines)
        validate_build_inputs(self.root, snapshot, engines)
        self.state["build"]["versions"] = snapshot_versions(
            snapshot, old, self.state["build"]["versions"]
        )
        bundles = dict(self.state["bundles"])
        for component in components:
            prior = bundles.get(component, {})
            bundles[component] = {
                "revision": str(uuid.uuid4()),
                "compile": component in compile_components
                or prior.get("compile", False),
            }
        self.state.update(inputs=snapshot.name, bundles=bundles)
        _write_json(self.directory / "install.json", self.state)

    def _rebuild(self, timeout: str) -> None:
        """Let platform installation own environment images and retain installed values."""
        from foretoken.platform import PlatformLifecycle

        settings = dict(self.state["command"])
        settings.update(values=(), timeout=timeout)
        command = InstallCommand(**settings)
        PlatformLifecycle(command.oci_registry).install(
            command,
            source_base_image=self.state.get("base_image"),
            source_build_arguments=self.state.get("build", {}).get("arguments"),
        )
        self.state = json.loads((self.directory / "install.json").read_text())
        self.selected.clear()

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
        self, namespace: str, bundles: dict[str, dict[str, Any]], timeout: str
    ) -> bool:
        """Compile on separate storage and publish to the workload cache before rollout."""
        from foretoken.source import ensure_build_cache, local_build_nodes

        runtime = self.state["runtime"]
        build = self.state["build"]
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
        # Keep first GPU-node placement with the model preparation controller.
        if (
            pvc.get("status", {}).get("phase") != "Bound"
            and "ReadWriteMany" not in pvc["spec"]["accessModes"]
        ):
            return False
        mounts = [
            pod
            for pod in self.kubectl.list_resources(("pods",), namespace)
            if pod["spec"].get("nodeName")
            and any(
                volume.get("persistentVolumeClaim", {}).get("claimName") == claim
                for volume in pod["spec"].get("volumes", [])
            )
        ]
        mounts.sort(
            key=lambda pod: (
                pod.get("status", {}).get("phase") != "Running",
                bool(pod["metadata"].get("deletionTimestamp")),
            )
        )
        node = mounts[0]["spec"]["nodeName"] if mounts else ""
        local_nodes = local_build_nodes(
            self.kubectl, build["registry"], build["containerd_socket"]
        )
        socket = local_nodes[0][1]
        if socket and not node:
            node = local_nodes[0][0]
        node_uid = (
            self.kubectl.get("node", node)["metadata"]["uid"][:8] if node else "shared"
        )
        build_claim = f"foretoken-source-build-{pvc['metadata']['uid'][:8]}-{node_uid}"
        ensure_build_cache(
            self.kubectl,
            namespace,
            build_claim,
            build["configuration"],
            owner={
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "name": claim,
                "uid": pvc["metadata"]["uid"],
            },
        )
        snapshot = self.directory / self.state["inputs"]
        files = {
            str(path.relative_to(snapshot)): path
            for path in snapshot.rglob("*")
            if path.is_file()
        }
        with ClusterBuilder(
            self.kubectl,
            namespace,
            build_claim,
            "/var/cache/foretoken-build",
            build["configuration"]["image"],
            build["binding"],
            timeout,
            node=node,
            containerd_socket=socket,
            pull_secrets=tuple(runtime["pull_secrets"]),
            publisher_image=runtime["model_image"],
            runtime_claim=claim,
            runtime_mount=runtime["mount"],
            credentials=registry_credentials(
                [
                    runtime["model_image"],
                    build["configuration"]["image"],
                    "docker.io",
                    "ghcr.io",
                    *(
                        value
                        for key, value in build["arguments"].items()
                        if key.endswith("REGISTRY")
                    ),
                ]
            ),
        ) as builder:
            builder.sync(files, build["versions"])
            for component, bundle in bundles.items():
                revision = bundle["revision"]
                destination = runtime["mount"] + "/source/" + revision
                if builder.read_json(
                    destination + "/complete.json", container="publisher"
                ):
                    continue
                staging = builder.root + "/output/" + revision
                payload = staging + "/payload"
                builder.run(["rm", "-rf", "--", staging])
                builder.run(["mkdir", "-p", payload])
                if bundle["compile"]:
                    builder.build(
                        f"data-plane/{component}/Dockerfile",
                        target="source-export",
                        destination=staging + "/runtime",
                        arguments=build["arguments"],
                    )
                    builder.run(
                        [
                            "sh",
                            "-ec",
                            'cp -R "$1/." "$2/"',
                            "assemble",
                            staging + "/runtime",
                            payload,
                        ]
                    )
                if component == "model-server":
                    builder.run(
                        [
                            "cp",
                            "-R",
                            builder.workspace + "/data-plane/model-server/python",
                            payload + "/python",
                        ]
                    )
                    if self.state.get("engines"):
                        builder.build(
                            "deploy/inference-engines/source-build.Dockerfile",
                            target="source-export",
                            destination=staging + "/engine",
                            arguments={
                                **build["arguments"],
                                "RUNTIME_IMAGE": runtime["model_image"],
                                "CACHE_ID": build["binding"]
                                + "-"
                                + build["environment"],
                                "BUILD_NATIVE": str(
                                    build.get("engine_native", False)
                                ).lower(),
                            },
                        )
                        builder.run(
                            [
                                "sh",
                                "-ec",
                                'cp -R "$1/." "$2/"',
                                "assemble",
                                staging + "/engine",
                                payload,
                            ]
                        )
                builder.publish(
                    payload,
                    destination,
                    {
                        "revision": revision,
                        "binding": build["binding"],
                        "component": component,
                        "executable": f"foretoken-{component}"
                        if bundle["compile"]
                        else None,
                    },
                )
                builder.run(["rm", "-rf", "--", staging])
            self._retire_sources(builder)
        return True

    def _retire_sources(self, builder: ClusterBuilder) -> None:
        """Retire this binding's unreferenced payloads while preserving shared-volume consumers."""
        # Namespaces may mount the same data directory. Include retained rollout
        # templates and terminating/preparation Pods, not just currently Ready services.
        objects = list(
            self.kubectl.list_all_resources(
                ("modelservice", "frontendservice", "modelpool", "modelgroup")
            )
        )
        for label in (
            "inference.foretoken.io/model-group",
            "inference.foretoken.io/frontend-service",
            "inference.foretoken.io/model-preparation-group",
        ):
            objects.extend(
                self.kubectl.list_all_resources(
                    ("pods", "jobs", "replicasets", "deployments"), label_selector=label
                )
            )
        keep = {bundle["revision"] for bundle in self.state["bundles"].values()}
        for obj in objects:
            spec = obj.get("spec", {})
            template = spec.get("template", {})
            keep.update(
                filter(
                    None,
                    (
                        obj["metadata"].get("annotations", {}).get(SOURCE_REVISION),
                        template.get("metadata", {})
                        .get("annotations", {})
                        .get(SOURCE_REVISION),
                        template.get("sourceRevision"),
                        spec.get("runtime", {}).get("sourceRevision"),
                    ),
                )
            )
            pod_spec = spec if obj["kind"] == "Pod" else template.get("spec", {})
            for container in (
                *pod_spec.get("containers", []),
                *pod_spec.get("initContainers", []),
            ):
                for variable in container.get("env", []):
                    if variable[
                        "name"
                    ] == "FORETOKEN_SOURCE_DIRECTORY" and variable.get("value"):
                        keep.add(variable["value"].rstrip("/").rsplit("/", 1)[-1])
        script = """import json, shutil, sys
from pathlib import Path
selection = json.load(sys.stdin)
root = Path(sys.argv[1])
for directory in root.iterdir():
    if not directory.is_dir() or directory.name in selection["keep"]:
        continue
    manifest = directory / "complete.json"
    if not manifest.is_file():
        continue
    try:
        bundle = json.loads(manifest.read_text())
        if isinstance(bundle, dict) and bundle.get("binding") == selection["binding"] and bundle.get("revision") == directory.name:
            shutil.rmtree(directory)
    except (OSError, ValueError) as error:
        print(f"Source cache cleanup: {directory.name}: {error}", file=sys.stderr)
"""
        builder.run(
            [
                "sh",
                "-ec",
                'exec "${FORETOKEN_VLLM_PYTHON:-python}" -c "$1" "$2"',
                "retire",
                script,
                self.state["runtime"]["mount"] + "/source",
            ],
            container="publisher",
            input_text=json.dumps(
                {"binding": self.state["build"]["binding"], "keep": sorted(keep)}
            ),
        )

    def _ready_containers(
        self, namespace: str, component: str, service: str, revision: str
    ) -> list[tuple[str, str, str, str]]:
        """Select ready containers and route identities from the committed source or image cohort."""
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
            route_target = ""
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
                route_target = group["metadata"]["uid"]
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
                    containers.append(
                        (metadata["name"], component, directory, route_target)
                    )
        if component == "model-server" and seen_pools != set(selected):
            return []
        return containers

    def _frontend_routes_ready(
        self,
        namespace: str,
        service: str,
        writers: list[tuple[str, str, str, str]],
        route_targets: set[str],
        deadline: float,
    ) -> bool:
        """Observe the existing routing publication, consumer acknowledgements and Service endpoints."""
        deployment = self.kubectl.get("deployment", service, namespace)
        config_name = next(
            volume["configMap"]["name"]
            for volume in deployment["spec"]["template"]["spec"]["volumes"]
            if volume["name"] == "serving"
        )
        config_map = self.kubectl.get_if_exists("configmap", config_name, namespace)
        if config_map is None:
            return False
        snapshot = json.loads(config_map["data"]["serving.json"])
        published = {
            route["route_target_id"]
            for section in ("groups", "pd_components", "epd_components")
            for route in (snapshot.get(section) or [])
        }
        if not route_targets <= published:
            return False
        pods = [
            pod
            for pod in self.kubectl.list_resources(("pods",), namespace)
            if not pod["metadata"].get("deletionTimestamp")
            and pod["metadata"]
            .get("labels", {})
            .get("inference.foretoken.io/frontend-service")
            == service
        ]
        if len(pods) < deployment["spec"]["replicas"] or {
            pod["metadata"]["name"] for pod in pods
        } != {name for name, _, _, _ in writers}:
            return False
        for pod in pods:
            container = next(
                c for c in pod["spec"]["containers"] if c["name"] == "frontend"
            )
            port = next(
                port["containerPort"]
                for port in container["ports"]
                if port["name"] == "http"
            )
            state = json.loads(
                self.kubectl.get_raw(
                    f"/api/v1/namespaces/{namespace}/pods/http:{pod['metadata']['name']}:{port}/proxy/statusz",
                    f"{max(1, int(deadline - time.monotonic()))}s",
                )
            )
            if (
                not state["serving_ready"]
                or state["active_generation"] != snapshot["version"]
            ):
                return False
        endpoints = {
            endpoint.get("targetRef", {}).get("uid")
            for value in self.kubectl.list_resources(("endpointslices",), namespace)
            if value["metadata"].get("labels", {}).get("kubernetes.io/service-name")
            == service
            for endpoint in value.get("endpoints", [])
            if endpoint.get("conditions", {}).get("ready") is True
        }
        return bool(pods) and {pod["metadata"]["uid"] for pod in pods} <= endpoints

    def verify(
        self, timeout: str, *, observe: Callable[[], None] | None = None
    ) -> None:
        """Wait for selected runtime code and for frontends to consume the matching routes."""
        deadline = time.monotonic() + timeout_seconds(timeout)
        routes: dict[str, set[str]] = {}
        # Source annotations do not advance Service generation. Verify committed
        # backend code first, then the routing consumers of that exact cohort.
        selected = sorted(
            self.selected.items(), key=lambda item: item[0][0] == "FrontendService"
        )
        for (kind, namespace, service), revision in selected:
            component = _COMPONENTS[kind]
            if component == "frontend":
                from foretoken.manifest import ResourceRef

                self.kubectl.rollout_status(
                    ResourceRef("Deployment", service, namespace),
                    f"{max(1, int(deadline - time.monotonic()))}s",
                )
            while True:
                if observe is not None:
                    observe()
                writers = self._ready_containers(
                    namespace, component, service, revision
                )
                if writers and (
                    component != "frontend"
                    or self._frontend_routes_ready(
                        namespace,
                        service,
                        writers,
                        routes.get(namespace, set()),
                        deadline,
                    )
                ):
                    break
                if time.monotonic() >= deadline:
                    raise DeploymentError(
                        f"timed out waiting for {namespace}/{service} to activate source {revision}"
                    )
                time.sleep(2)
            if component == "model-server":
                routes.setdefault(namespace, set()).update(
                    route for _, _, _, route in writers
                )
            for pod, container, directory, _ in writers:
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
