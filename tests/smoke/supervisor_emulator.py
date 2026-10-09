"""Supervisor stand-in for the smoke test (DESIGN v3.2 §12.2 item 11, 15).

Answers the three API-bypass calls the add-on makes, as the Supervisor does:
  GET  /info                 {"result":"ok","data":{"supervisor":…,"homeassistant":…}}
  GET  /addons/self/info     {"result":"ok","data":{"options":…,"ingress_entry":…,"hostname":…}}
  POST /addons/self/options  replaces the options (the real API replaces them as a whole)
and, for the test only, GET /__test/state (no auth) with what was posted.

Environment: SUP_TOKEN, SUP_VERSION, CORE_VERSION, INGRESS_ENTRY (empty = none),
OPTIONS_JSON (initial options), SILENT_SECONDS (answer 503 for that long after start).
Runs inside the add-on image (stdlib only): python supervisor_emulator.py [port]
"""

import http.server
import json
import os
import sys
import threading
import time

TOKEN = os.environ["SUP_TOKEN"]
VERSIONS = {"supervisor": os.environ.get("SUP_VERSION", "2026.10.1"), "homeassistant": os.environ.get("CORE_VERSION", "2026.10.0")}
ENTRY = os.environ.get("INGRESS_ENTRY", "")
SILENT_UNTIL = time.monotonic() + float(os.environ.get("SILENT_SECONDS", "0"))
STATE = {"options": json.loads(os.environ.get("OPTIONS_JSON", "{}")), "posts": [], "calls": []}
LOCK = threading.Lock()


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"supervisor-emulator: {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}", file=sys.stderr, flush=True)

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        with LOCK:
            STATE["calls"].append({"method": self.command, "path": self.path, "t": time.time()})
        if time.monotonic() < SILENT_UNTIL:
            self._send(503, {"result": "error", "message": "starting"})
            return False
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send(401, {"result": "error", "message": "Missing or invalid token"})
            return False
        return True

    def do_GET(self):
        if self.path == "/__test/state":
            with LOCK:
                self._send(200, STATE)
            return
        if not self._authorized():
            return
        if self.path == "/info":
            self._send(200, {"result": "ok", "data": {**VERSIONS, "arch": "amd64", "hostname": "homeassistant"}})
        elif self.path == "/addons/self/info":
            with LOCK:
                data = {"name": "Woow LiteLLM", "slug": "woow-litellm", "hostname": "1b7b4ce7-woow-litellm",
                        "ingress": True, "options": dict(STATE["options"])}
            if ENTRY:
                data["ingress_entry"] = ENTRY
            self._send(200, {"result": "ok", "data": data})
        else:
            self._send(404, {"result": "error", "message": "not found"})

    def do_POST(self):
        if not self._authorized():
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/addons/self/options" and isinstance(body.get("options"), dict):
            with LOCK:
                STATE["options"] = dict(body["options"])
                STATE["posts"].append(body)
            self._send(200, {"result": "ok", "data": {}})
        else:
            self._send(400, {"result": "error", "message": "bad request"})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 80
    http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
