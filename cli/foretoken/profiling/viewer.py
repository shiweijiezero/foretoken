# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Serve retained capture history on loopback using the caller's cluster access."""

from __future__ import annotations

import json
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlsplit

from foretoken.arguments import ProfileViewCommand
from foretoken.kubernetes import Kubectl, timeout_seconds
from foretoken.manifest import DeploymentError
from foretoken.profiling.reader import CAPTURE_DIRECTORY
from foretoken.profiling.storage import ProfileStorage


class CaptureDirectories:
    """Discover capture storage; retained run records enrich files but do not gate access."""

    def __init__(self, kubectl: Kubectl, namespace: str | None) -> None:
        self.kubectl = kubectl
        self.namespace = namespace
        self.stores: dict[str, dict[str, str]] = {}
        self.runs: tuple[dict[str, Any], ...] = ()

    def refresh(self) -> dict[str, Any]:
        """List cache roots and select the latest successful capture's storage."""
        kinds = (
            "runtimecaches.inference.foretoken.io",
            "profileruns.inference.foretoken.io",
        )
        objects = (
            self.kubectl.list_resources(kinds, self.namespace)
            if self.namespace is not None
            else self.kubectl.list_all_resources(kinds)
        )
        stores = {}
        runs = []
        for item in objects:
            metadata, status = item["metadata"], item.get("status", {})
            namespace = metadata["namespace"]
            if item["kind"] == "RuntimeCache":
                claim = status.get("claimName")
                if claim:
                    key = f"{namespace}/{claim}"
                    stores[key] = {
                        "id": key,
                        "namespace": namespace,
                        "claim": claim,
                        "name": metadata["name"],
                    }
            elif status.get("artifact"):
                runs.append(item)
        runs.sort(
            key=lambda run: (
                run.get("status", {}).get("startedAt")
                or run["metadata"]["creationTimestamp"]
            ),
            reverse=True,
        )
        latest = None
        for run in runs:
            namespace = run["metadata"]["namespace"]
            claim = run["status"]["artifact"]["claimName"]
            key = f"{namespace}/{claim}"
            stores.setdefault(
                key, {"id": key, "namespace": namespace, "claim": claim, "name": claim}
            )
            if latest is None and run["status"].get("phase") == "Succeeded":
                latest = key
        self.stores, self.runs = stores, tuple(runs)
        return {
            "stores": sorted(stores.values(), key=lambda store: store["id"]),
            "latest": latest,
        }

    def directory(self, store_id: str) -> tuple[str, str, str]:
        """Resolve only discovered storage, never a browser-supplied PVC identity."""
        store = self.stores.get(store_id)
        if store is None:
            raise FileNotFoundError(
                "Capture storage is no longer listed; refresh the page."
            )
        return store["namespace"], store["claim"], CAPTURE_DIRECTORY

    def files(self, store_id: str, storage: ProfileStorage) -> list[dict[str, Any]]:
        """Recursively list saved traces, attaching historical metadata where available."""
        namespace, claim, root = self.directory(store_id)
        runs = [
            run
            for run in self.runs
            if run["metadata"]["namespace"] == namespace
            and run["status"]["artifact"]["claimName"] == claim
        ]
        entries = storage.list_files(namespace, claim, root)
        for entry in entries:
            path = f"{root}/{entry['name']}"
            run = next(
                (
                    run
                    for run in runs
                    if path.startswith(
                        run["status"]["artifact"]["path"].rstrip("/") + "/"
                    )
                ),
                None,
            )
            if run is not None:
                status = run["status"]
                entry.update(
                    {
                        "model": (status.get("plan") or {}).get("model"),
                        "service": run["spec"]["modelServiceRef"]["name"],
                        "status": status.get("phase", "Pending"),
                        "time": status.get("startedAt")
                        or run["metadata"]["creationTimestamp"],
                    }
                )
        return entries


class ProfileViewer(ThreadingHTTPServer):
    """Accept browser preconnections while serializing cluster access and draining on exit."""

    daemon_threads = False

    def __init__(self, history: CaptureDirectories, storage: ProfileStorage) -> None:
        self.cluster_lock = threading.Lock()
        self.connection_lock = threading.Lock()
        self.connections: set[socket.socket] = set()
        self.closing = threading.Event()
        self.history = history
        self.storage = storage
        self.session_path = "/" + secrets.token_urlsafe(24) + "/"
        self.page = (
            resources.files("foretoken.profiling").joinpath("viewer.html").read_bytes()
        )
        super().__init__(("127.0.0.1", 0), ProfileHandler)
        self.origin = f"http://{self.server_address[0]}:{self.server_port}"

    def get_request(self) -> tuple[socket.socket, Any]:
        """Track accepted sockets so idle browser connections cannot prevent shutdown."""
        connection, address = super().get_request()
        with self.connection_lock:
            self.connections.add(connection)
        return connection, address

    def shutdown_request(self, request: socket.socket) -> None:
        """Forget completed connections before the server drains its remaining threads."""
        with self.connection_lock:
            self.connections.discard(request)
        super().shutdown_request(request)

    def server_close(self) -> None:
        """Close browser connections and join request handlers before reader cleanup."""
        self.closing.set()
        with self.connection_lock:
            for connection in self.connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass  # The browser may already have disconnected.
        super().server_close()


class ProfileHandler(BaseHTTPRequestHandler):
    """Expose deployment history and authenticated trace bytes to the local browser."""

    server: ProfileViewer

    def log_message(self, format: str, *args: object) -> None:
        # URLs contain the local session credential.
        pass

    def _headers(
        self, status: int, content_type: str, length: int | None = None
    ) -> None:
        self.send_response(status)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-src https://ui.perfetto.dev; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        self.end_headers()

    def _json(self, status: int, value: object) -> None:
        self._headers(status, "application/json; charset=utf-8")
        self.wfile.write(json.dumps(value).encode())

    def do_GET(self) -> None:
        """Handle same-origin viewer requests and stream one selected trace."""
        # Reject DNS rebinding and cross-origin requests before acquiring cluster resources.
        if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
            self.send_error(403)
            return
        if self.headers.get("Origin", self.server.origin) != self.server.origin:
            self.send_error(403)
            return
        parsed = urlsplit(self.path)
        if not parsed.path.startswith(self.server.session_path):
            self.send_error(404)
            return
        with self.server.cluster_lock:
            if self.server.closing.is_set():
                return
            try:
                self._serve(parsed.path[len(self.server.session_path) :], parsed.query)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    def _serve(self, route: str, query_string: str) -> None:
        """Handle one authorized request with exclusive access to the capture inventory."""
        streaming = False
        try:
            if not route:
                self._headers(200, "text/html; charset=utf-8")
                self.wfile.write(self.server.page)
            elif route == "api/stores":
                self._json(200, self.server.history.refresh())
            elif route in {"api/files", "api/trace"}:
                query = parse_qs(query_string)
                store_id = query.get("store", [""])[0]
                artifact = self.server.history.directory(store_id)
                files = self.server.history.files(store_id, self.server.storage)
                if route == "api/files":
                    self._json(200, files)
                    return
                name = query.get("file", [""])[0]
                selected = next((item for item in files if item["name"] == name), None)
                if selected is None:
                    raise FileNotFoundError("Trace file is no longer available.")
                self._headers(200, "application/octet-stream", selected["size"])
                streaming = True
                self.server.storage.stream_file(*artifact, name, self.wfile)
            else:
                self.send_error(404)
        except (FileNotFoundError, DeploymentError) as error:
            if streaming:
                # A partial trace must not be followed by a JSON error body.
                self.close_connection = True
            else:
                self._json(
                    404 if isinstance(error, FileNotFoundError) else 503,
                    {"error": str(error)},
                )


def view(command: ProfileViewCommand) -> None:
    """Print a loopback viewer URL and retain readers until the user exits."""
    if timeout_seconds(command.timeout) <= 0:
        raise DeploymentError("--timeout must be positive")
    kubectl = Kubectl()
    # A long-lived viewer must not switch clusters when another terminal changes context.
    context = kubectl.run(["config", "current-context"]).stdout.strip()
    kubectl = Kubectl(context=context)
    history = CaptureDirectories(kubectl, command.namespace)
    history.refresh()
    with (
        ProfileStorage(kubectl, timeout=command.timeout) as storage,
        ProfileViewer(history, storage) as server,
    ):
        print(f"Profile viewer: {server.origin}{server.session_path}", flush=True)
        print("Press Ctrl+C to stop.", flush=True)
        server.serve_forever()
