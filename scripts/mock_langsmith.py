#!/usr/bin/env python3
"""Tiny mock LangSmith endpoint for offline testing: logs run payloads.

Handles the multipart/form-data ingestion endpoint (each part is one JSON
run) that the langsmith client uses when compression is disabled.
"""
import json
import sys
from email.parser import BytesParser
from email import policy
from http.server import BaseHTTPRequestHandler, HTTPServer

LOG = []


def _runs_from_multipart(content_type: str, body: bytes):
    msg = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    for part in msg.iter_parts():
        try:
            yield json.loads(part.get_payload())
        except Exception:
            pass


def _usage(run):
    out = (run or {}).get("outputs") or {}
    return out.get("usage") if isinstance(out, dict) else None


def _latency(run):
    r = run or {}
    if r.get("end_time"):
        return "end_time set"
    out = r.get("outputs") or {}
    return out.get("latency_seconds") if isinstance(out, dict) else None


class H(BaseHTTPRequestHandler):
    def _reply(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_runs(self, runs):
        current = None
        for part in runs:
            if not isinstance(part, dict):
                continue
            if part.get("id") and part.get("name"):
                current = dict(part)
                LOG.append(current)
                print(f"RUN: {current.get('name')} [{current.get('run_type')}] "
                      f"usage={_usage(current)} latency={_latency(current)} "
                      f"error={(current.get('error') or None)}", flush=True)
            elif current is not None and any(
                    k in part for k in ("output", "usage", "latency_seconds")):
                current.setdefault("outputs", {}).update(part)
                print(f"    outputs: {json.dumps(part)[:160]}", flush=True)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        ct = self.headers.get("Content-Type") or ""
        try:
            if "multipart" in ct:
                self._handle_runs(_runs_from_multipart(ct, body))
            elif self.path.endswith("/batch"):
                self._handle_runs(json.loads(body or b"{}").get("runs", []))
            else:
                self._handle_runs([json.loads(body or b"{}")])
        except Exception as exc:
            print(f"parse error on {self.path}: {exc}", flush=True)
        self._reply({})

    def do_GET(self):
        self._reply({})

    def do_PATCH(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            print(f"PATCH: {json.loads(self.rfile.read(n) or b'{}')}", flush=True)
        except Exception:
            pass
        self._reply({})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
