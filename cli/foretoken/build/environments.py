# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Retain dependency environments independently of checkout compiler caches."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from loguru import logger

from foretoken.build.cluster.builder import ClusterBuilder, ImageNotFoundError
from foretoken.build.records import BUILD_TRANSPORT_ARGUMENTS


class BuildEnvironments:
    """Own dependency-stage images and their delivery across the selected build nodes."""

    def __init__(
        self,
        builders: list[ClusterBuilder],
        versions: dict[str, str],
        previous: dict[str, Any],
        images: dict[str, str],
        *,
        push: bool,
        refresh: bool = False,
    ) -> None:
        self.builders = builders
        self.versions = versions
        self.previous = previous
        self.images = images
        self.push = push
        self.refresh = refresh
        self.records: dict[str, Any] = {}
        self._built: set[str] = set()
        self._delivered: dict[str, set[str]] = {builder.node: set() for builder in builders}

    def build(
        self,
        name: str,
        dockerfile: str,
        *,
        target: str = "",
        arguments: dict[str, str],
        input_prefixes: tuple[str, ...],
        node_arguments: dict[str, dict[str, str]] | None = None,
        execution_builder: ClusterBuilder | None = None,
    ) -> dict[str, Any]:
        """Build and retain a stage on its execution node, or all runtime nodes when omitted.

        Source revisions identify local inputs; OCI descriptors identify selected
        image contents. BuildKit remains responsible for the stage's layer cache.
        Compiler-cache addresses are execution details, not environment inputs.
        """
        builders = [execution_builder] if execution_builder is not None else self.builders
        inputs = {
            path: version
            for path, version in self.versions.items()
            if path in {".dockerignore", dockerfile, dockerfile + ".dockerignore"}
            or path.startswith(input_prefixes)
        }
        identity: dict[str, Any] = {
            "dockerfile": dockerfile,
            "target": target,
            "inputs": inputs,
            "arguments": {
                key: value for key, value in arguments.items()
                if not key.endswith("_IMAGE") and key not in BUILD_TRANSPORT_ARGUMENTS | {"CACHE_ID"}
            },
            "nodes": {},
        }
        for builder in builders:
            overrides = (node_arguments or {}).get(builder.node, {})
            identity["nodes"]["" if self.push else builder.node] = {
                "platform": builder.run(["uname", "-m"], capture=True).strip(),
                "arguments": {
                    key: value for key, value in overrides.items() if key != "CACHE_ID"
                },
                "images": {
                    key: value if value == "scratch" else builder.resolve_image(value)
                    for key, value in arguments.items()
                    if key.endswith("_IMAGE")
                },
            }
        previous = self.previous.get(name, {})
        if previous:
            if self.push:
                # A registry-backed origin can relocate without changing its image.
                digest = next(iter(previous["digests"].values()))
                previous = {**previous, "digests": {builder.node: digest for builder in builders}}
            # Retain matching image references when fewer nodes need this stage.
            previous = {
                **previous,
                "identity": {
                    **previous["identity"],
                    "nodes": {
                        node: value for node, value in previous["identity"]["nodes"].items()
                        if node in identity["nodes"]
                    },
                },
                "digests": {
                    builder.node: previous["digests"][builder.node]
                    for builder in builders if builder.node in previous["digests"]
                },
            }
        reusable = not self.refresh and previous.get("identity") == identity
        if reusable and self._available(previous, builders):
            self.records[name] = previous
            logger.info("Reusing {} environment: {}", name, previous["image"])
            return previous

        image = self.images[name]
        with ThreadPoolExecutor(max_workers=len(builders)) as builds, builders[0].cancellation:
            pending = {
                builder.node: builds.submit(
                    builder.cancellation.run_stage, builder.build,
                    dockerfile, target=target, image=image, push=self.push,
                    arguments={**arguments, **(node_arguments or {}).get(builder.node, {})},
                )
                for builder in builders
            }
            digests = {
                node: future.result()["containerimage.digest"]
                for node, future in pending.items()
            }
        if previous.get("digests") == digests and self._available(previous, builders):
            image = previous["image"]
        else:
            self._built.add(name)
        record = {"identity": identity, "image": image, "digests": digests}
        self.records[name] = record
        return record

    def deliver(self, builder: ClusterBuilder, names: set[str] | None = None) -> None:
        """Deliver completed stages once per node, leaving borrowed image references with their owner."""
        if builder.containerd_socket:
            pending = {
                name for name in self._built - self._delivered[builder.node]
                if builder.node in self.records[name]["digests"]
            }
            if names is not None:
                pending &= names
            # The controller runtime is delivered last, immediately before installation.
            for name in sorted(pending, key=lambda value: value == "control-plane"):
                image = self.records[name]["image"]
                builder.import_image(image, image)
                self._delivered[builder.node].add(name)

    def _available(self, record: dict[str, Any], builders: list[ClusterBuilder]) -> bool:
        """Check the stage's execution nodes before borrowing a producer's retained image reference."""
        for builder in builders:
            try:
                digest = record["digests"].get(builder.node)
                if digest is None or not builder.image_matches(record["image"], digest):
                    return False
            except ImageNotFoundError as error:
                logger.warning("Environment image was removed; rebuilding: {}", error)
                return False
        return True
