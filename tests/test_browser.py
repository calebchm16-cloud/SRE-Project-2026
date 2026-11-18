"""Exercise real navigation and CSS with JavaScript disabled; all providers are local."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import payload
from playwright.sync_api import sync_playwright


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.browser
def test_traditional_html_navigation_and_degradation(tmp_path):
    modes = {"weather": "ok", "marine": "ok"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            source = "marine" if self.path.startswith("/v1/marine") else "weather"
            self.send_response(503 if modes[source] == "error" else 200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload(source, datetime.now(UTC))).encode())

        def log_message(self, *args):
            pass

    mock = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=mock.serve_forever, daemon=True)
    thread.start()
    port = unused_port()
    locations = tmp_path / "locations.json"
    locations.write_text(
        json.dumps(
            [
                {
                    "id": "cahuita",
                    "name": "Cahuita",
                    "latitude": 9.74,
                    "longitude": -82.84,
                    "timezone": "America/Costa_Rica",
                },
                {
                    "id": "test-site",
                    "name": "Test location",
                    "latitude": 10,
                    "longitude": -82,
                    "timezone": "UTC",
                },
            ]
        )
    )
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("CAHUITA_", "OTEL_"))
    }
    env.update(
        CAHUITA_LOCATIONS_FILE=str(locations),
        CAHUITA_DATABASE_PATH=str(tmp_path / "cache.db"),
        CAHUITA_WEATHER_BASE_URL=f"http://127.0.0.1:{mock.server_port}",
        CAHUITA_MARINE_BASE_URL=f"http://127.0.0.1:{mock.server_port}",
        CAHUITA_REFRESH_SECONDS="0.3",
        CAHUITA_STALE_SECONDS="1.2",
        CAHUITA_EXPIRE_SECONDS="4",
        CAHUITA_HEARTBEAT_SECONDS="0.05",
        CAHUITA_RETRY_DELAY_SECONDS="0",
        CAHUITA_REQUEST_TIMEOUT_SECONDS="0.2",
    )
    with (tmp_path / "app.log").open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "cahuita_conditions.app:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-access-log",
            ],
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            for _ in range(100):
                try:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/health/ready", timeout=1
                    ).close()
                    break
                except OSError, urllib.error.HTTPError:
                    assert process.poll() is None, (tmp_path / "app.log").read_text()
                    time.sleep(0.05)
            else:
                pytest.fail((tmp_path / "app.log").read_text())
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                context = browser.new_context(
                    java_script_enabled=False, viewport={"width": 1280, "height": 900}
                )
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(error))
                response = page.goto(f"http://127.0.0.1:{port}/")
                assert response.status == 200
                assert page.locator("script").count() == 0
                assert page.locator("table.table-condensed.table-bordered").count() == 2
                assert page.locator("table").nth(1).locator("tbody tr").count() == 48
                assert (
                    page.locator("body").evaluate("e => getComputedStyle(e).backgroundColor")
                    == "rgb(255, 255, 255)"
                )
                assert (
                    page.locator("button").evaluate("e => getComputedStyle(e).borderTopColor")
                    == "rgb(204, 204, 204)"
                )
                assert page.locator("#location").input_value() == "cahuita"
                page.select_option("#location", "test-site")
                with page.expect_navigation():
                    page.get_by_role("button", name="Show / refresh forecast").click()
                assert page.url.endswith("/?location=test-site")
                assert page.locator("#location").input_value() == "test-site"
                assert "Times: UTC" in page.locator("main").inner_text()
                modes["marine"] = "error"
                time.sleep(1.5)
                page.reload()
                assert "Degraded forecast" in page.locator("main").inner_text()
                assert "stale" in page.locator("table").first.inner_text()
                assert not errors
                page.goto(f"http://127.0.0.1:{port}/?location=cahuita")
                artifact = Path("artifacts/bootstrap3-page.png")
                artifact.parent.mkdir(exist_ok=True)
                page.screenshot(path=str(artifact), full_page=True)
                browser.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            mock.shutdown()
            mock.server_close()
            thread.join(timeout=2)
