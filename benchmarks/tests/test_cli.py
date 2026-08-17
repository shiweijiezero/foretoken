# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from benchmarks.main import main


class _OpenAIHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        if request.get("model") == "fail-model":
            self.send_error(503, "unavailable")
            return
        if request.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                {
                    "id": "completion-1",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": request["model"],
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "hello"},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "completion-1",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": request["model"],
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 2,
                        "completion_tokens": 1,
                        "total_tokens": 3,
                    },
                },
            ]
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return

        body = json.dumps(
            {
                "id": "completion-1",
                "object": "chat.completion",
                "created": 1,
                "model": request["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hello"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def _openai_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize("stream", [True, False])
def test_direct_run_writes_redacted_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "secret-token")
    with _openai_server() as base_url:
        args = [
            "bench",
            "run",
            "--base-url",
            base_url,
            "--model",
            "test-model",
            "--prompt",
            "hello",
            "--num-requests",
            "2",
            "--max-concurrency",
            "2",
            "--output-dir",
            str(tmp_path),
            "--run-id",
            f"direct-{'stream' if stream else 'nonstream'}",
        ]
        if not stream:
            args.append("--no-stream")
        assert main(args) == 0

    output = tmp_path / f"direct-{'stream' if stream else 'nonstream'}"
    config = json.loads((output / "config.json").read_text())
    metrics = json.loads((output / "metrics.json").read_text())
    manifest = json.loads((output / "manifest.json").read_text())
    assert config["target"]["api_key"] == "<redacted>"
    assert "secret-token" not in (output / "config.json").read_text()
    assert metrics["success_num"] == 2
    assert metrics["request_num"] == 2
    assert manifest == {
        "run_id": f"direct-{'stream' if stream else 'nonstream'}",
        "execution_context": "endpoint",
        "resources_owned": False,
        "phase": "completed",
    }


def test_all_failed_requests_return_nonzero_and_keep_results(tmp_path: Path) -> None:
    with _openai_server() as base_url:
        exit_code = main(
            [
                "bench",
                "run",
                "--base-url",
                base_url,
                "--model",
                "fail-model",
                "--num-requests",
                "1",
                "--output-dir",
                str(tmp_path),
                "--run-id",
                "direct-failed",
            ]
        )

    assert exit_code == 1
    output = tmp_path / "direct-failed"
    metrics = json.loads((output / "metrics.json").read_text())
    manifest = json.loads((output / "manifest.json").read_text())
    assert metrics["success_num"] == 0
    assert metrics["failed_num"] == 1
    assert manifest["phase"] == "failed"
