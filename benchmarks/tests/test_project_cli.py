# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from benchmarks.main import main

_MODEL = "Qwen/Qwen3-0.6B"


class _OpenAIHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def _json(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") == "Bearer EMPTY":
            return True
        self.send_error(401)
        return False

    def do_GET(self) -> None:
        if self.path == "/v1/models" and self._authorized():
            self._json(
                {
                    "object": "list",
                    "data": [
                        {"id": _MODEL, "object": "model", "owned_by": "foretoken"}
                    ],
                }
            )
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        if not self._authorized():
            return
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        self._json(
            {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": request["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Hello"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        )


def _start_server() -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _fake_kubectl(directory: Path, port: int) -> tuple[Path, Path]:
    binary = directory / "kubectl"
    log = directory / "kubectl.log"
    binary.write_text(
        f"""#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "{log}"
case "$1 $2" in
  "kustomize "*)
    cat <<'YAML'
apiVersion: v1
kind: Namespace
metadata:
  name: foretoken-demo
---
apiVersion: inference.foretoken.io/v1alpha1
kind: FrontendService
metadata:
  name: quickstart-frontend
spec:
  replicas: 1
  resources: {{}}
  timeouts: {{}}
---
apiVersion: inference.foretoken.io/v1alpha1
kind: ModelService
metadata:
  name: quickstart-model
spec:
  model: {_MODEL}
YAML
    ;;
  "wait --for=condition=Ready")
    ;;
  "get service")
    cat <<'JSON'
{{"spec":{{"type":"LoadBalancer","ports":[{{"name":"http","port":{port}}}]}},"status":{{"loadBalancer":{{"ingress":[{{"ip":"127.0.0.1"}}]}}}}}}
JSON
    ;;
  *)
    echo "unexpected kubectl command: $*" >&2
    exit 1
    ;;
esac
""",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary, log


def _project(directory: Path) -> Path:
    project = directory / "project"
    project.mkdir()
    (project / "kustomization.yaml").write_text(
        "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n",
        encoding="utf-8",
    )
    return project


def _assert_result(output: Path) -> None:
    metrics = list(output.glob("*/metrics.json"))
    assert len(metrics) == 1
    payload = json.loads(metrics[0].read_text(encoding="utf-8"))
    assert payload["success_num"] == 1
    config = json.loads((metrics[0].parent / "config.json").read_text(encoding="utf-8"))
    assert "api_key" not in config["target"]


def test_project_and_existing_endpoint_commands(tmp_path: Path, monkeypatch) -> None:
    server, thread = _start_server()
    try:
        port = server.server_address[1]
        _, kubectl_log = _fake_kubectl(tmp_path, port)
        monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
        project = _project(tmp_path)

        project_output = tmp_path / "project-results"
        main(
            [
                "bench",
                str(project),
                "--wait-timeout",
                "5s",
                "--number",
                "1",
                "--no-stream",
                "--outputs-dir",
                str(project_output),
            ]
        )
        _assert_result(project_output)

        direct_output = tmp_path / "direct-results"
        main(
            [
                "bench",
                "--url",
                f"http://127.0.0.1:{port}/v1/chat/completions",
                "--model",
                _MODEL,
                "--prompt",
                "Hello",
                "--number",
                "1",
                "--no-stream",
                "--outputs-dir",
                str(direct_output),
            ]
        )
        _assert_result(direct_output)

        commands = kubectl_log.read_text(encoding="utf-8")
        assert "apply" not in commands
        assert "wait --for=condition=Ready" in commands
        assert "get service quickstart-frontend -o json" in commands
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
