#!/usr/bin/env python3
"""Capture real demo UIs and an explicitly simulated notification receipt."""

import argparse
import hashlib
import html
import json
import shutil
import sys
import urllib.parse
from datetime import UTC, datetime

import failure_demo_env as env
from playwright.sync_api import sync_playwright


def receiver_html(state, snapshot, status):
    events = [
        event
        for event in snapshot["messages"]
        if event["code"] == 200
        and event["text"].startswith(status)
        and "Alert: CahuitaForecastsUnavailable\n" in event["text"]
        and f"Namespace: {state['namespace']}\n" in event["text"]
    ]
    if not events:
        return None
    event = events[-1]
    received = datetime.fromtimestamp(event["timestamp"], UTC).isoformat()
    title = f"{status} — SIMULATED DELIVERY"
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<title>{title}</title><link rel="stylesheet" href="bootstrap.min.css">
<body><main class="container"><h3>{title}</h3>
<p>Local Telegram Bot API mock. This is a real recorded receipt from Alertmanager,
not a message delivered to a private Telegram account.</p>
<table class="table table-condensed table-bordered"><tbody>
<tr><th>Run</th><td>{html.escape(state["run_id"])}</td></tr>
<tr><th>Namespace</th><td>{html.escape(state["namespace"])}</td></tr>
<tr><th>Received (UTC)</th><td>{received}</td></tr>
<tr><th>HTTP result</th><td>{event["code"]}</td></tr>
<tr><th>Completed successful requests at capture</th>
<td>{snapshot["notifications"]["successful"]}</td></tr>
<tr><th>Real Telegram acceptance</th><td>Pending bot configuration</td></tr>
</tbody></table><h4>Recorded message</h4><pre>{html.escape(event["text"])}</pre>
<p>Both FIRING and RESOLVED receipts refer to this run's critical forecast-loss condition.</p>
</main></body></html>"""


def capture(run_id, phase, urls):
    state = env.load(run_id)
    directory = env.directory(run_id)
    output = directory / "screenshots"
    output.mkdir(exist_ok=True)
    data = json.loads((directory / f"{phase}.json").read_text())
    password = env.grafana_password(state)
    records, query_errors = [], []
    vendor = env.k8s.ROOT / "src/cahuita_conditions/static/vendor"
    for filename in ("bootstrap.min.css", "BOOTSTRAP-LICENSE.txt"):
        shutil.copyfile(vendor / filename, output / filename)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            application = browser.new_context(
                java_script_enabled=False,
                viewport={"width": 1600, "height": 1300},
                timezone_id="America/Costa_Rica",
            )
            app = application.new_page()
            app.goto(urls["app"] + "/?location=cahuita", wait_until="load")
            app.get_by_role("heading", name="Cahuita Conditions", exact=False).wait_for()
            text = app.locator("body").inner_text()
            assert (
                "table-condensed table-bordered" in app.content()
                and 'method="get"' in app.content()
            )
            expected = {
                "healthy": "Status: ok",
                "cached": "Status: degraded",
                "partial": "Status: degraded",
                "unavailable": "Forecast unavailable.",
                "recovered": "Status: ok",
            }[phase]
            assert expected in text, (phase, expected)

            def save(page, filename, origin):
                path = output / filename
                page.screenshot(path=str(path), full_page=True)
                (output / (path.stem + ".txt")).write_text(page.locator("body").inner_text())
                record = {
                    "file": "screenshots/" + filename,
                    "run_id": run_id,
                    "phase": phase,
                    "captured_at": datetime.now(UTC).isoformat(),
                    "origin": origin,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                records.append(record)

            save(app, env.CAPTURES[phase][0], "application, JavaScript disabled")
            context = browser.new_context(
                viewport={"width": 1600, "height": 1800}, timezone_id="America/Costa_Rica"
            )
            page = context.new_page()

            def response(response):
                if "/api/ds/query" not in response.url:
                    return
                try:
                    body = response.json()
                    if response.status >= 400 or any(
                        result.get("error") for result in body.get("results", {}).values()
                    ):
                        query_errors.append({"status": response.status, "body": body})
                except Exception as exc:
                    query_errors.append({"error": str(exc)})

            page.on("response", response)
            if phase == "cached":
                page.goto(urls["prometheus"] + "/alerts", wait_until="domcontentloaded")
                page.get_by_text("CahuitaRepeatedRefreshFailures", exact=False).first.wait_for(
                    timeout=20000
                )
                save(page, "08-prometheus-firing.png", "Prometheus alerts UI")
            elif phase in {"unavailable", "recovered"}:
                page.goto(urls["alertmanager"] + "/", wait_until="domcontentloaded")
                if phase == "unavailable":
                    page.get_by_text("CahuitaForecastsUnavailable", exact=False).first.wait_for(
                        timeout=20000
                    )
                else:
                    page.get_by_text("No alert groups found", exact=False).first.wait_for(
                        timeout=20000
                    )
                filename = (
                    "09-alertmanager-firing.png"
                    if phase == "unavailable"
                    else "10-alertmanager-resolved.png"
                )
                save(page, filename, "Alertmanager alerts UI")
                status = "FIRING" if phase == "unavailable" else "RESOLVED"
                message = receiver_html(state, data, status)
                if message is not None:
                    view = output / (status.lower() + "-receipt.html")
                    view.write_text(message)
                    page.goto(view.as_uri(), wait_until="load")
                    filename = (
                        "11-simulated-firing.png"
                        if status == "FIRING"
                        else "12-simulated-resolved.png"
                    )
                    save(page, filename, "recorded local mock receipt / SIMULATED DELIVERY")
                elif phase == "unavailable":
                    raise RuntimeError("No accepted critical FIRING receipt for this run")
            if phase == "recovered":
                page.goto(urls["grafana"] + "/login", wait_until="domcontentloaded")
                page.locator('input[name="user"]').fill("admin")
                page.locator('input[name="password"]').fill(password)
                page.get_by_role("button", name="Log in", exact=True).click()
                page.wait_for_url(lambda url: "/login" not in url, timeout=30000)
                params = urllib.parse.urlencode(
                    {
                        "var-namespace": state["namespace"],
                        "from": str(
                            int(datetime.fromisoformat(state["created_at"]).timestamp() * 1000)
                        ),
                        "to": str(
                            int(datetime.fromisoformat(data["captured_at"]).timestamp() * 1000)
                        ),
                        "refresh": "",
                    }
                )
                for uid, slug, panel, filename in [
                    (
                        "cahuita-application",
                        "application-traffic",
                        "Requests / second",
                        "06-traffic-history.png",
                    ),
                    (
                        "cahuita-forecasts",
                        "forecast-operations",
                        "Forecast data usable",
                        "07-forecast-history.png",
                    ),
                ]:
                    page.goto(
                        urls["grafana"] + f"/d/{uid}/{slug}?" + params,
                        wait_until="domcontentloaded",
                    )
                    page.get_by_text(panel, exact=True).wait_for(timeout=30000)
                    page.wait_for_load_state("networkidle", timeout=20000)
                    page.wait_for_timeout(1000)
                    save(page, filename, "isolated Grafana / fixed full-run time range")
            assert not query_errors, query_errors
            context.close()
            application.close()
            browser.close()
        (directory / f"{phase}-screenshots.json").write_text(json.dumps(records, indent=2) + "\n")
        return records
    except Exception as exc:
        raise RuntimeError(str(exc).replace(password, "[REDACTED]")) from None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--phase", choices=env.PHASES, required=True)
    parser.add_argument("--urls", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(capture(args.run_id, args.phase, json.loads(args.urls))))
    except Exception as exc:
        sys.exit(f"Demo browser error: {exc}")
