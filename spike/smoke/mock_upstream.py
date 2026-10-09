#!/usr/bin/env python3
"""Tiny OpenAI-compatible stand-in so the smoke test can reach real code paths.

POST /v1/responses          -> a Responses API object; status "queued" when the request
                               has "background": true (what OpenAI returns), else "completed"
POST /v1/chat/completions   -> a minimal chat completion
Listens on 127.0.0.1 only. Logs one line per request (method, path, background flag).

usage: mock_upstream.py PORT
"""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def responses_obj(model: str, background: bool) -> dict:
    now = int(time.time())
    status = "queued" if background else "completed"
    output = [] if background else [{
        "type": "message", "id": "msg_mock_1", "status": "completed", "role": "assistant",
        "content": [{"type": "output_text", "text": "pong", "annotations": []}],
    }]
    return {
        "id": f"resp_mock_{now}", "object": "response", "created_at": now, "status": status,
        "background": background, "error": None, "incomplete_details": None, "instructions": None,
        "max_output_tokens": None, "model": model, "output": output, "parallel_tool_calls": True,
        "previous_response_id": None, "reasoning": {"effort": None, "summary": None}, "store": True,
        "temperature": 1.0, "text": {"format": {"type": "text"}}, "tool_choice": "auto", "tools": [],
        "top_p": 1.0, "truncation": "disabled",
        "usage": None if background else {"input_tokens": 1, "input_tokens_details": {"cached_tokens": 0},
                                          "output_tokens": 1, "output_tokens_details": {"reasoning_tokens": 0},
                                          "total_tokens": 2},
        "user": None, "metadata": {},
    }


def chat_obj(model: str) -> dict:
    return {
        "id": "chatcmpl-mock", "object": "chat.completion", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class H(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            req = {}
        bg = bool(req.get("background"))
        print(f"mock: POST {self.path} model={req.get('model')} background={bg}", flush=True)
        if self.path.rstrip("/").endswith("/responses"):
            self._send(200, responses_obj(req.get("model", "mock"), bg))
        elif self.path.rstrip("/").endswith("/chat/completions"):
            self._send(200, chat_obj(req.get("model", "mock")))
        else:
            self._send(404, {"error": {"message": f"mock: no route {self.path}"}})

    def do_GET(self):  # noqa: N802
        print(f"mock: GET {self.path}", flush=True)
        self._send(404, {"error": {"message": f"mock: no route {self.path}"}})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
