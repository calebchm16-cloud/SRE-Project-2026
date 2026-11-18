#!/usr/bin/env python3
"""Validation-only Telegram Bot API. Fake credentials and messages stay in its namespace."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

STATE = Path("/control")
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/events":
            path = STATE / "events.jsonl"
            with LOCK:
                events = (
                    [json.loads(line) for line in path.read_text().splitlines()]
                    if path.exists()
                    else []
                )
            self.reply(events)
        else:
            self.reply({"ok": True})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        data = (
            json.loads(body)
            if self.headers.get("Content-Type", "").startswith("application/json")
            else {key: values[0] for key, values in parse_qs(body.decode()).items()}
        )
        if self.path.endswith("/getMe"):
            self.reply(
                {"ok": True, "result": {"id": 1, "is_bot": True, "first_name": "Validation"}}
            )
            return
        if not self.path.endswith("/sendMessage"):
            self.reply({"ok": True, "result": []})
            return
        with LOCK:
            control = STATE / "mode.json"
            mode = json.loads(control.read_text()) if control.exists() else {}
            queue = mode.get("failures", [])
            code = queue.pop(0) if queue else 200
            if control.exists():
                control.write_text(json.dumps({"failures": queue}))
            event = {
                "timestamp": time.time(),
                "code": code,
                "text": data.get("text", ""),
                "chat_id": str(data.get("chat_id", "")),
            }
            with (STATE / "events.jsonl").open("a") as output:
                output.write(json.dumps(event) + "\n")
        if code != 200:
            self.reply(
                {
                    "ok": False,
                    "error_code": code,
                    "description": "Simulated delivery failure",
                    "parameters": {"retry_after": 1},
                },
                status=code,
            )
        else:
            self.reply(
                {
                    "ok": True,
                    "result": {
                        "message_id": time.time_ns() % 1000000,
                        "date": int(time.time()),
                        "chat": {"id": 1, "type": "private"},
                        "text": event["text"],
                    },
                }
            )

    def reply(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    STATE.mkdir(exist_ok=True)
    ThreadingHTTPServer(("0.0.0.0", 9001), Handler).serve_forever()
