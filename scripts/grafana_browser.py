#!/usr/bin/env python3
"""Authenticated browser checks and screenshots; never persist cookies or credentials."""

import argparse
import json
import sys
import urllib.parse

import grafana
from playwright.sync_api import sync_playwright


def run(namespace="cahuita-conditions", phase="normal"):
    output = grafana.k8s.ROOT / "artifacts/grafana"
    output.mkdir(parents=True, exist_ok=True)
    errors, query_errors, queried = [], [], []
    with grafana.forward() as client, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1600, "height": 1800}, timezone_id="America/Costa_Rica"
        )
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))

        def response_received(response):
            if "/api/ds/query" not in response.url:
                return
            queried.append(response.status)
            if response.status >= 400:
                query_errors.append({"status": response.status, "body": response.json()})
                return
            try:
                data = response.json()
                for ref, result in data.get("results", {}).items():
                    if result.get("error"):
                        query_errors.append({"refId": ref, "error": result["error"]})
            except Exception as exc:
                query_errors.append({"response_error": str(exc)})

        page.on("response", response_received)
        page.goto(client.url + "/login", wait_until="domcontentloaded")
        page.locator('input[name="user"]').fill("admin")
        page.locator('input[name="password"]').fill(grafana.password())
        page.get_by_role("button", name="Log in", exact=True).click()
        page.wait_for_url(lambda url: "/login" not in url, timeout=30000)
        screenshots = []
        for uid, slug, title in [
            ("cahuita-application", "application-traffic", "Application Traffic"),
            ("cahuita-forecasts", "forecast-operations", "Forecast Operations"),
        ]:
            params = {"var-namespace": namespace, "from": "now-15m", "to": "now"}
            page.goto(
                client.url + f"/d/{uid}/{slug}?" + urllib.parse.urlencode(params),
                wait_until="domcontentloaded",
            )
            page.get_by_text(title, exact=True).first.wait_for(timeout=30000)
            # Grafana defers offscreen queries. Visit the entire canvas before the screenshot.
            page.wait_for_timeout(3000)
            height = page.evaluate("document.documentElement.scrollHeight")
            for y in range(0, height, 700):
                page.evaluate("(y) => window.scrollTo(0, y)", y)
                page.wait_for_timeout(250)
            page.wait_for_timeout(1500)
            page.evaluate("window.scrollTo(0, 0)")
            filename = f"{phase}-{slug}.png"
            page.screenshot(path=str(output / filename), full_page=True)
            screenshots.append(filename)
            text = page.locator("body").inner_text()
            (output / f"{phase}-{slug}.txt").write_text(text)
            for message in [
                "Templating error",
                "Panel plugin not found",
                "Error loading dashboard",
                "An unexpected error happened",
            ]:
                assert message not in text, message
            assert "Exporter target" in text
            if uid == "cahuita-application":
                assert "Requests / second" in text and "Client errors (4xx)" in text
            else:
                assert "Forecast data usable" in text and "Available forecast hours" in text
                if phase == "expired":
                    assert "Unavailable" in text and "UP" in text
            # Dashboard links must point to the other stable UID and retain variables/time.
            other = "cahuita-forecasts" if uid == "cahuita-application" else "cahuita-application"
            link = page.locator(f'a[href*="/d/{other}/"]').first
            assert link.count(), f"Missing dashboard link to {other}"
            href = link.get_attribute("href")
            assert "var-namespace=" in href and "from=" in href, href

        if phase == "normal":
            # Exercise URL-backed filters through Grafana's actual frontend interpolation.
            page.goto(
                client.url
                + "/d/cahuita-application/application-traffic?"
                + urllib.parse.urlencode(
                    {"var-namespace": namespace, "var-route": "/health/ready"}
                ),
                wait_until="domcontentloaded",
            )
            page.get_by_text("Requests / second", exact=True).wait_for(timeout=30000)
            page.wait_for_timeout(2500)
            assert "/health/ready" in page.locator("body").inner_text()
            page.goto(
                client.url
                + "/d/cahuita-application/application-traffic?"
                + urllib.parse.urlencode(
                    {
                        "var-namespace": namespace,
                        "var-route": "/api/v1/locations/{location_id}/forecast",
                    }
                ),
                wait_until="domcontentloaded",
            )
            page.get_by_text("Requests / second", exact=True).wait_for(timeout=30000)
            page.wait_for_timeout(2500)
            assert "/api/v1/locations/{location_id}/forecast" in page.locator("body").inner_text()
            page.goto(
                client.url
                + "/d/cahuita-forecasts/forecast-operations?"
                + urllib.parse.urlencode({"var-namespace": namespace, "var-source": "weather"}),
                wait_until="domcontentloaded",
            )
            page.get_by_text("Forecast data usable", exact=True).wait_for(timeout=30000)
            page.wait_for_timeout(2500)
            assert "weather" in page.locator("body").inner_text()
        context.close()
        browser.close()
    assert queried, "No real Grafana panel requests were observed"
    assert not query_errors, query_errors
    assert not errors, errors
    result = {
        "namespace": namespace,
        "phase": phase,
        "screenshots": screenshots,
        "panel_query_requests": len(queried),
        "query_errors": query_errors,
        "javascript_errors": errors,
        "login": "passed",
        "links": "passed",
    }
    (output / f"{phase}-browser.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="cahuita-conditions")
    parser.add_argument("--phase", choices=["normal", "expired", "recovered"], default="normal")
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.namespace, args.phase), indent=2))
    except Exception as exc:
        # Playwright call logs may contain fill() values. Redact before emitting any failure.
        message = str(exc).replace(grafana.password(), "[REDACTED]")
        sys.exit(f"Browser validation failed: {message}")
