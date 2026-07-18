"""Offline contract demo for the Langfuse and Phoenix adapters.

This starts a tiny local HTTP server with current provider response shapes and
runs the real SkillPulse HTTP clients, mappers, CLI, and SQLite store. It needs
no provider accounts and sends no data outside the machine.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from skillpulse.cli import main
from skillpulse.store import SkillStore


class _ContractHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        payload = _response(path)
        if payload is None:
            self.send_error(404)
            return
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args) -> None:
        pass


def _response(path: str) -> dict | None:
    if path == "/api/public/v2/observations":
        return {
            "data": [{
                "id": "langfuse-root",
                "traceId": "lf-demo",
                "projectId": "demo-project",
                "parentObservationId": None,
                "startTime": "2026-07-01T12:00:00Z",
                "name": "support-agent",
                "level": "DEFAULT",
                "input": {"question": "Where is my order?"},
                "output": {"answer": "It ships today."},
                "metadata": {},
            }],
            "meta": {},
        }
    if path == "/api/public/v3/scores":
        return {
            "data": [{
                "name": "correctness",
                "value": 0.96,
                "source": "EVAL",
                "subject": {"kind": "trace", "id": "lf-demo"},
            }],
            "meta": {},
        }
    if path == "/v1/projects/support-demo/spans":
        return {
            "data": [{
                "name": "support-agent",
                "context": {"trace_id": "ph-demo", "span_id": "root"},
                "span_kind": "AGENT",
                "start_time": "2026-07-01T12:01:00Z",
                "status_code": "OK",
                "attributes": {
                    "input.value": "{\"question\": \"Can I cancel?\"}",
                    "output.value": "{\"answer\": \"Yes, before shipment.\"}",
                },
                "events": [],
            }],
            "next_cursor": None,
        }
    if path == "/v1/projects/support-demo/trace_annotations":
        return {
            "data": [{
                "name": "correctness",
                "trace_id": "ph-demo",
                "annotator_kind": "CODE",
                "result": {"label": "pass", "score": 0.92},
            }],
            "next_cursor": None,
        }
    if path == "/v1/projects/support-demo/span_annotations":
        return {"data": [], "next_cursor": None}
    return None


def _run(db_path: str, args: list[str]) -> None:
    display = ["$PROVIDER_URL" if value.startswith("http://127.0.0.1:") else value
               for value in args]
    print(f"$ skillpulse {' '.join(display)}")
    main(["--db", db_path, *args])


def run_demo() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ContractHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    previous = {name: os.environ.get(name) for name in (
        "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "PHOENIX_API_KEY")}
    os.environ.update({
        "LANGFUSE_PUBLIC_KEY": "demo-public",
        "LANGFUSE_SECRET_KEY": "demo-secret",
        "PHOENIX_API_KEY": "demo-key",
    })
    try:
        with tempfile.TemporaryDirectory(prefix="skillpulse-demo-") as directory:
            db_path = os.path.join(directory, "demo.db")
            print("SkillPulse observability adapter contract demo\n")
            _run(db_path, ["add", "support", "--name", "Support agent"])
            print()
            _run(db_path, [
                "sync", "langfuse", "--base-url", base_url,
                "--skill-id", "support", "--success-score", "correctness",
                "--since", "30d",
            ])
            print()
            _run(db_path, [
                "sync", "phoenix", "--base-url", base_url,
                "--project", "support-demo", "--skill-id", "support",
                "--success-score", "correctness", "--since", "30d",
            ])
            print()
            _run(db_path, ["status"])
            store = SkillStore(db_path)
            runs = store.get_skill_runs("support", 1)
            evidence = ", ".join(
                f"{run.source} correctness={run.evaluations['correctness']['value']}"
                for run in runs)
            store.close()
            print(f"\nPreserved evaluation evidence: {evidence}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


if __name__ == "__main__":
    run_demo()
