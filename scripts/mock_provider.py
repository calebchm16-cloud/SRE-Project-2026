#!/usr/bin/env python3
"""External, development-only Open-Meteo stub controlled by a local JSON file.

Example file: {"weather": "error", "marine": "ok", "delay_seconds": 12}
Modes: ok, error (503), timeout, rate_limit (429), malformed, empty, nulls.
This server is deliberately separate from the application and has no control API.
"""

import argparse
import json
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def payload(query):
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    fields = query.get("hourly", [""])[0].split(",")
    values = {
        "temperature_2m": ("°C", 27.5),
        "precipitation_probability": ("%", 35.0),
        "precipitation": ("mm", 0.2),
        "wind_speed_10m": ("km/h", 9.0),
        "wind_direction_10m": ("°", 80.0),
        "wind_gusts_10m": ("km/h", 14.0),
        "wave_height": ("m", 0.8),
        "wave_period": ("s", 7.0),
        "wave_direction": ("°", 65.0),
    }
    return {
        "latitude": float(query.get("latitude", [9.74])[0]),
        "longitude": float(query.get("longitude", [-82.84])[0]),
        "hourly_units": {"time": "unixtime", **{field: values[field][0] for field in fields}},
        "hourly": {
            "time": [int((now + timedelta(hours=i)).timestamp()) for i in range(72)],
            **{field: [values[field][1]] * 72 for field in fields},
        },
    }


def handler(mode_file):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path)
            if path.path not in {"/v1/forecast", "/v1/marine"}:
                self.send_error(404)
                return
            source = "weather" if path.path == "/v1/forecast" else "marine"
            try:
                modes = (
                    json.loads(mode_file.read_text()) if mode_file and mode_file.exists() else {}
                )
                mode = modes.get(source, "ok")
                if mode == "timeout":
                    time.sleep(modes.get("delay_seconds", 12))
                status = {"error": 503, "rate_limit": 429}.get(mode, 200)
                data = payload(parse_qs(path.query))
                if mode == "empty":
                    data["hourly"] = {key: [] for key in data["hourly"]}
                if mode == "nulls":
                    for field in data["hourly"]:
                        if field != "time":
                            data["hourly"][field] = [None] * 72
                body = b"{bad json" if mode == "malformed" else json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if mode == "rate_limit":
                    self.send_header("Retry-After", str(modes.get("retry_after", 30)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, fmt, *args):
            pass

    return Handler


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--mode-file", type=Path)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), handler(args.mode_file))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
