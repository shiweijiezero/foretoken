# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Prepare source-installed runtime updates without rebuilding their environment images."""

from __future__ import annotations

import copy
import filecmp
import json
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from foretoken.application_files import SOURCE_REVISION, ApplicationFiles
from foretoken.arguments import InstallCommand
from foretoken.cluster_build import (
    ClusterBuilder,
    find_build_cache,
    registry_credentials,
)
from foretoken.kubernetes import Kubectl, timeout_seconds
from foretoken.manifest import (
    DeploymentError,
    ForetokenDeployment,
    ResourceRef,
    parse_deployment,
)
from foretoken.platform import PlatformLifecycle
from foretoken.platform.config import default_platform_config
from foretoken.platform.helm import Helm
from foretoken.source import (
    _INSTALL_SOURCE,
    _has_server_binding,
    _inputs,
    _local_candidates,
    _runtime_settings,
    _snapshot,
    _state_directory,
    _write_json,
    ensure_build_cache,
    image_tools_image,
    local_build_nodes,
    pinned_rust_revision,
    snapshot_versions,
    validate_build_inputs,
)

_COMPONENTS = {"ModelService": "model-server", "FrontendService": "frontend"}


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
        rebuild = False
        for name in changed:
            if name.startswith("engine/"):
                if Path(name).suffix in {".md", ".png", ".svg"}:
                    continue
                parts = Path(name).parts
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
            elif name.startswith("control-plane/") and Path(name).name != "Dockerfile":
                components.add("control-plane")
            elif name.startswith("data-plane/model-server/python/") and name.endswith(
                ".py"
            ):
                components.add("model-server")
            elif name.startswith("data-plane/") and (
                name.endswith(".rs") or Path(name).name in {"Cargo.toml", "Cargo.lock"}
            ):
                targets = (
                    {"frontend"}
                    if name.startswith("data-plane/frontend/")
                    else {"model-server"}
                    if name.startswith("data-plane/model-server/")
                    else {"frontend", "model-server"}
                )
                components.update(targets)
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
        _snapshot(
            current,
            snapshot,
            engines,
            previous=old,
            unchanged=set(current).difference(changed),
        )
        validate_build_inputs(self.root, snapshot, engines)
        self.state["build"]["versions"] = snapshot_versions(
            snapshot, old, self.state["build"]["versions"], changed
        )
        bundles = dict(self.state["bundles"])
        for component in components:
            prior = bundles.get(
                component,
                self.state["build"].get("applications", {}).get(component, {}),
            )
            bundles[component] = {
                "revision": str(uuid.uuid4()),
                "previous": prior.get("revision", ""),
            }
        self.state.update(inputs=snapshot.name, bundles=bundles)
        _write_json(self.directory / "install.json", self.state)

    def _rebuild(self, timeout: str) -> None:
        """Let platform installation own environment images and retain installed values."""
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
        """Publish selected applications before submitting service and model-storage intent."""
        objects = copy.deepcopy(deployment.objects)
        pending: set[str] = set()
        for obj in objects:
            component = _COMPONENTS.get(obj.get("kind"))
            if component is None:
                continue
            metadata = obj["metadata"]
            annotations = metadata.setdefault("annotations", {})
            annotations.pop(SOURCE_REVISION, None)
            bundle = self.state["bundles"].get(component)
            namespace = metadata.get("namespace") or deployment.namespace or "default"
            if (
                bundle is not None
                and component == "model-server"
                and obj["spec"].get("backend") != "vllm"
            ):
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
                .get(SOURCE_REVISION, "")
            )
            revision = bundle["revision"] if bundle is not None else ""
            writers = (
                self._ready_containers(namespace, component, metadata["name"], revision)
                if current is not None
                else []
            )
            ready = bool(writers)
            if component == "frontend":
                ready = ready and len(writers) == obj["spec"].get("replicas", 1)
            # Platform image changes do not advance Service generation; old Ready status
            # must not make a deployment finish before the selected runtime is serving.
            if active != revision or not ready:
                self.selected[(obj["kind"], namespace, metadata["name"])] = revision
            if bundle is not None:
                if active != revision:
                    pending.add(component)
                annotations[SOURCE_REVISION] = revision
        if pending or "control-plane" in self.state["bundles"]:
            self._publish_applications(pending, timeout)
        return parse_deployment(
            deployment.path, yaml.safe_dump_all(objects, sort_keys=False)
        )

    def _publish_applications(self, components: set[str], timeout: str) -> None:
        """Publish selected components once, independently of their model namespaces and caches."""
        platforms = self.kubectl.list_all_resources(
            ("deployments",),
            label_selector="app.kubernetes.io/name=foretoken-control-plane",
        )
        platform = next(
            item
            for item in platforms
            if item["metadata"]["uid"] == self.state["platform_uid"]
        )
        namespace = platform["metadata"]["namespace"]
        origin = ApplicationFiles(self.kubectl, namespace)
        pending = set(components)
        control = self.state["bundles"].get("control-plane")
        control_reference = (
            origin.reference("control-plane", control["revision"]) if control else ""
        )
        active_control = (
            platform["spec"]["template"]["metadata"]
            .get("annotations", {})
            .get("inference.foretoken.io/application-url", "")
        )
        if control_reference and active_control != control_reference:
            pending.add("control-plane")
        if not pending:
            return

        origin.prepare(timeout)
        build = self.state["build"]
        mount = "/var/cache/foretoken"
        claim = find_build_cache(
            self.kubectl, namespace, build["binding"], origin.node, mount
        )
        local_caches = local_build_nodes(
            self.kubectl, build["registry"], build["containerd_socket"]
        )
        socket = next(
            (socket for node, socket, _ in local_caches if node == origin.node), ""
        )
        if claim is None:
            node_uid = self.kubectl.get("node", origin.node)["metadata"]["uid"][:8]
            claim = next(
                (claim for node, _, claim in local_caches if node == origin.node),
                "foretoken-application-build-" + node_uid,
            )
        ensure_build_cache(
            self.kubectl,
            namespace,
            claim,
            build["configuration"],
            owner={
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "name": origin.claim,
                "uid": origin.claim_uid,
            },
        )
        if (
            "model-server" in pending
            and self.state.get("engines")
            and not build["registry"]
            and origin.node not in build["engine_caches"]
        ):
            # A newly prepared origin node needs a cold cache, not another node's native outputs.
            build["engine_caches"][origin.node] = (
                build["binding"] + "-" + uuid.uuid4().hex
            )
            _write_json(self.directory / "install.json", self.state)
        snapshot = self.directory / self.state["inputs"]
        inputs = {
            str(path.relative_to(snapshot)): path
            for path in snapshot.rglob("*")
            if path.is_file()
        }
        helm = Helm(default_platform_config(self.state["command"]["oci_registry"]))
        references = origin.references(helm.application_history())
        if references is not None:
            references.update(
                bundle["revision"] for bundle in self.state["bundles"].values()
            )
        print("Preparing application files: " + ", ".join(sorted(pending)), flush=True)
        with ClusterBuilder(
            self.kubectl,
            namespace,
            claim,
            mount,
            build["configuration"]["image"],
            build["binding"],
            timeout,
            tools_image=image_tools_image(
                build["arguments"], build.get("registry_mirrors", {})
            ),
            registry_mirrors=build.get("registry_mirrors", {}),
            node=origin.node,
            containerd_socket=socket
            if self.state.get("engines") and "model-server" in pending
            else "",
            credentials=registry_credentials(
                [
                    build["configuration"]["image"],
                    self.state["runtime"]["model_image"],
                    self.state.get("base_image") or "",
                    build["arguments"].get("UV_IMAGE", ""),
                    "docker.io",
                    "gcr.io",
                    "ghcr.io",
                    *(
                        endpoint
                        for endpoints in build.get("registry_mirrors", {}).values()
                        for endpoint in endpoints
                    ),
                    *(
                        value
                        for key, value in build["arguments"].items()
                        if key.endswith("REGISTRY")
                    ),
                ]
            ),
            pull_secrets=origin.pull_secrets,
        ) as builder:
            builder.sync(inputs, build["versions"])
            for component in sorted(pending):
                bundle = self.state["bundles"][component]
                revision = bundle["revision"]
                staging = builder.root + "/output/" + revision
                payload = staging + "/payload"
                dockerfile = (
                    "control-plane/Dockerfile"
                    if component == "control-plane"
                    else f"data-plane/{component}/Dockerfile"
                )
                builder.build(
                    dockerfile,
                    target="source-export",
                    destination=payload,
                    arguments=build["arguments"],
                )
                if component == "model-server" and self.state.get("engines"):
                    builder.build(
                        "deploy/inference-engines/source-build.Dockerfile",
                        target="source-export",
                        destination=staging + "/engine",
                        arguments={
                            **build["arguments"],
                            "RUNTIME_IMAGE": self.state["runtime"]["model_image"],
                            "CACHE_ID": build["engine_caches"][
                                "" if build["registry"] else origin.node
                            ],
                            "BUILD_NATIVE": str(build["engine_native"]).lower(),
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
                validate_build_inputs(
                    self.root, snapshot, self.state.get("engines", {})
                )
                previous = bundle.get("previous", "")
                if component == "control-plane" and active_control.startswith(
                    origin.endpoint + "/control-plane/"
                ):
                    previous = active_control.rsplit("/", 1)[-1]
                origin.publish(
                    builder,
                    payload,
                    component,
                    revision,
                    previous,
                    references,
                    timeout=timeout,
                )
                builder.run(["rm", "-rf", "--", staging])
        if "control-plane" in pending:
            helm.update_control_plane_application(snapshot, control_reference, timeout)
            self.kubectl.rollout_status(
                ResourceRef("Deployment", platform["metadata"]["name"], namespace),
                timeout,
            )

    def _ready_containers(
        self, namespace: str, component: str, service: str, revision: str
    ) -> list[tuple[str, str, str, str]]:
        """Select ready containers and route identities from the committed source or image cohort."""
        containers = []
        selected = {}
        pool_sizes: dict[str, int] = {}
        group_sizes: dict[str, int] = {}
        ready_members: dict[str, dict[str, int]] = {}
        if component == "model-server":
            current = self.kubectl.get("modelservice", service, namespace)
            selected = {
                p["poolUID"]: p["revision"]
                for p in current.get("status", {}).get("servingPoolRevisions", [])
            }
        else:
            current = self.kubectl.get("frontendservice", service, namespace)
            application = current.get("status", {}).get("application")
            if application is not None:
                frontend_image = application["image"]
            else:
                deployment = self.kubectl.get_if_exists(
                    "deployment", service, namespace
                )
                if deployment is None:
                    return []
                frontend_image = deployment["spec"]["template"]["spec"]["containers"][
                    0
                ]["image"]
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
            expected_image = frontend_image if component == "frontend" else ""
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
                expected_image = runtime["image"]
                pool_uid = pool["metadata"]["uid"]
                pool_sizes[pool_uid] = pool["spec"]["desiredGroups"]
                route_target = group["metadata"]["uid"]
                group_sizes[route_target] = group["spec"]["memberCount"]
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
                    if component == "model-server":
                        members = ready_members.setdefault(pool_uid, {})
                        members[route_target] = members.get(route_target, 0) + 1
        if component == "model-server":
            if set(ready_members) != set(selected):
                return []
            if any(
                len(groups) != pool_sizes[pool_uid]
                or any(
                    count != group_sizes[group_uid]
                    for group_uid, count in groups.items()
                )
                for pool_uid, groups in ready_members.items()
            ):
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
        self,
        deployment: ForetokenDeployment,
        timeout: str,
        *,
        observe: Callable[[], None] | None = None,
    ) -> None:
        """Wait for changed runtime code and its frontend routing consumers to become active."""
        deadline = time.monotonic() + timeout_seconds(timeout)
        routes: dict[str, set[str]] = {}
        consumers = dict(self.selected)
        # Unchanged frontend code still needs to consume a new backend cohort.
        namespaces = {
            namespace for kind, namespace, _ in self.selected if kind == "ModelService"
        }
        for namespace in namespaces:
            for frontend in self.kubectl.list_resources(
                ("frontendservices",), namespace
            ):
                metadata = frontend["metadata"]
                if (
                    not metadata.get("deletionTimestamp")
                    and frontend["spec"].get("replicas", 1) > 0
                ):
                    consumers.setdefault(
                        ("FrontendService", namespace, metadata["name"]),
                        metadata.get("annotations", {}).get(SOURCE_REVISION, ""),
                    )
        # Verify committed backend code first, then the routing consumers of that cohort.
        selected = sorted(
            consumers.items(), key=lambda item: item[0][0] == "FrontendService"
        )
        for (kind, namespace, service), revision in selected:
            component = _COMPONENTS[kind]
            code_changed = (kind, namespace, service) in self.selected
            if component == "frontend" and code_changed:
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
            if not code_changed:
                continue
            for pod, container, directory, _ in writers:
                expected = (
                    f"FORETOKEN_ACTIVE_SOURCE_DIRECTORY={directory}"
                    if directory
                    else ""
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
