"""A small HTTP memory service whose process state is snapshotted by Substrate."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
import json
import threading
from typing import Any

MAX_TEXT_LENGTH = 4000
MAX_TURNS = 20
MAX_REQUEST_BYTES = MAX_TEXT_LENGTH * 6 + 1024
_turns: list[dict[str, str]] = []
_turns_lock = threading.Lock()


def append_turn(role: str, text: str) -> list[dict[str, str]]:
    if role not in {"user", "assistant"}:
        raise ValueError("role must be user or assistant")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_LENGTH:
        raise ValueError(f"text must contain 1 to {MAX_TEXT_LENGTH} characters")

    with _turns_lock:
        _turns.append({"role": role, "text": text})
        del _turns[:-MAX_TURNS]
        return [dict(turn) for turn in _turns]


class MemoryHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/readyz":
            self._send_json(200, {"ready": True})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        roles = {"/turn": "user", "/reply": "assistant"}
        role = roles.get(self.path)
        if role is None:
            self._send_json(404, {"error": "not found"})
            return
        try:
            size = int(self.headers.get("Content-Length", ""))
            if not 0 < size <= MAX_REQUEST_BYTES:
                raise ValueError("invalid request size")
            body: Any = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError("request must be a JSON object")
            turns = append_turn(role, body.get("text"))
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(200, {"turns": turns})

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 80), MemoryHandler).serve_forever()
