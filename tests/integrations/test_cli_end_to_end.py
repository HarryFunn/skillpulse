from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from skillpulse.cli import main
from skillpulse.store import SkillStore


class _ProviderHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.auth_headers.append(self.headers.get("Authorization", ""))
        path = self.path.split("?", 1)[0]
        if path == "/api/public/v2/observations":
            payload = {
                "data": [{
                    "id": "langfuse-root",
                    "traceId": "lf-e2e",
                    "projectId": "project-e2e",
                    "parentObservationId": None,
                    "startTime": "2026-07-01T12:00:00Z",
                    "name": "support-agent",
                    "level": "DEFAULT",
                    "input": {"question": "hello"},
                    "output": {"answer": "hi"},
                    "metadata": {},
                }],
                "meta": {},
            }
        elif path == "/api/public/v3/scores":
            payload = {
                "data": [{
                    "name": "correctness",
                    "value": 1.0,
                    "subject": {"kind": "trace", "id": "lf-e2e"},
                }],
                "meta": {},
            }
        elif path == "/v1/projects/support-project/spans":
            payload = {
                "data": [{
                    "name": "support-agent",
                    "context": {"trace_id": "ph-e2e", "span_id": "root"},
                    "span_kind": "AGENT",
                    "start_time": "2026-07-01T12:01:00Z",
                    "status_code": "OK",
                    "attributes": {},
                    "events": [],
                }],
                "next_cursor": None,
            }
        elif path == "/v1/projects/support-project/trace_annotations":
            payload = {
                "data": [{
                    "name": "correctness",
                    "trace_id": "ph-e2e",
                    "annotator_kind": "CODE",
                    "result": {"label": "pass", "score": 1.0},
                }],
                "next_cursor": None,
            }
        elif path == "/v1/projects/support-project/span_annotations":
            payload = {"data": [], "next_cursor": None}
        else:
            self.send_error(404)
            return
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        pass


def test_cli_syncs_both_providers_over_real_http(tmp_path, monkeypatch, capsys):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderHandler)
    server.auth_headers = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    db_path = str(tmp_path / "e2e.db")
    try:
        main(["--db", db_path, "add", "support", "--name", "Support"])
        capsys.readouterr()

        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        main([
            "--db", db_path, "sync", "langfuse",
            "--base-url", base_url,
            "--skill-id", "support",
            "--success-score", "correctness",
            "--since", "30d",
        ])
        langfuse_output = capsys.readouterr().out

        monkeypatch.setenv("PHOENIX_API_KEY", "ph-test")
        main([
            "--db", db_path, "sync", "phoenix",
            "--base-url", base_url,
            "--project", "support-project",
            "--skill-id", "support",
            "--success-score", "correctness",
            "--since", "30d",
        ])
        phoenix_output = capsys.readouterr().out
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert "langfuse sync complete" in langfuse_output
    assert "SkillRuns   : added=1 duplicates=0 skipped=0" in langfuse_output
    assert "phoenix sync complete" in phoenix_output
    assert "SkillRuns   : added=1 duplicates=0 skipped=0" in phoenix_output
    basic = base64.b64encode(b"pk-test:sk-test").decode("ascii")
    assert f"Basic {basic}" in server.auth_headers
    assert "Bearer ph-test" in server.auth_headers

    store = SkillStore(db_path)
    runs = store.get_skill_runs("support", 1)
    assert [run.source for run in runs] == ["langfuse", "phoenix"]
    assert all(run.evaluations["correctness"]["value"] == 1.0 for run in runs)
    store.close()
