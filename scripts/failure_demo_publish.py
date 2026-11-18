"""Publish a narrow, credential-free evidence set from one verified completed run."""

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import failure_demo_env as env

DESTINATION = env.k8s.ROOT / "docs/evidence/failure-recovery"


def critical_receipt(state, snapshot, status):
    for event in reversed(snapshot["messages"]):
        text = event["text"]
        if (
            event["code"] == 200
            and text.startswith(status)
            and "Alert: CahuitaForecastsUnavailable\n" in text
            and f"Namespace: {state['namespace']}\n" in text
            and "Location: cahuita\n" in text
        ):
            fields = dict(line.split(": ", 1) for line in text.splitlines() if ": " in line)
            return {
                "status": status,
                "alertname": "CahuitaForecastsUnavailable",
                "namespace": state["namespace"],
                "location_id": "cahuita",
                "http_result": 200,
                "received_timestamp": event["timestamp"],
                "started_at": fields.get("Started (UTC)"),
                "ended_at": fields.get("Ended (UTC)"),
            }
    raise ValueError("Missing correlated simulated critical alert/resolve delivery")


def validate(state):
    if (
        state.get("result") != "passed"
        or state.get("cleanup") != "passed"
        or state.get("preserved") is not True
    ):
        raise ValueError(
            "Only a successful, cleaned run with preserved real workloads can be published"
        )
    if list(state["phases"]) != list(env.PHASES):
        raise ValueError("All five verified phases must belong to one ordered run")
    directory = env.directory(state["run_id"])
    activation_file = directory / "rule-activation.json"
    if not activation_file.is_file():
        raise ValueError("Missing verified rule activation after successful source scrapes")
    activation = json.loads(activation_file.read_text())
    if activation["startup_notifications"] != 0:
        raise ValueError("Startup must not generate notifications")
    records = [image for phase in env.PHASES for image in state["phases"][phase]["images"]]
    for phase in env.PHASES:
        for record in state["phases"][phase]["images"]:
            if record["phase"] != phase or record["file"] not in {
                "screenshots/" + filename for filename in env.CAPTURES[phase]
            }:
                raise ValueError("Screenshot phase differs from the stage in which it was captured")
    expected = {"screenshots/" + filename for group in env.CAPTURES.values() for filename in group}
    if {record["file"] for record in records} != expected or len(records) != 12:
        raise ValueError("Exactly the twelve selected evidence images are required")
    snapshots = {}
    for phase in env.PHASES:
        snapshot = json.loads((directory / state["phases"][phase]["snapshot"]).read_text())
        if (
            snapshot["run_id"] != state["run_id"]
            or snapshot["namespace"] != state["namespace"]
            or snapshot["phase"] != phase
            or snapshot["notification_channel"] != "simulated_telegram_api"
        ):
            raise ValueError("Snapshot provenance or simulated-delivery designation differs")
        snapshots[phase] = snapshot
    if snapshots["healthy"]["messages"]:
        raise ValueError("Healthy startup must have no simulated notification receipts")
    for record in records:
        if record["run_id"] != state["run_id"] or record["phase"] not in env.PHASES:
            raise ValueError("Screenshot belongs to another run")
        path = directory / record["file"]
        if not path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Evidence must be a real PNG capture")
        if hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError("Screenshot hash differs from its captured manifest")
    cached = snapshots["cached"]["responses"]["forecast"]["body"]["marine"]
    if cached["last_success_at"] != state["cached_download_at"] or not cached["hourly"]:
        raise ValueError("Cached-download continuity was not proven")
    first = critical_receipt(state, snapshots["unavailable"], "FIRING")
    last = critical_receipt(state, snapshots["recovered"], "RESOLVED")
    if not first["started_at"] or first["started_at"] != last["started_at"] or not last["ended_at"]:
        raise ValueError("FIRING and RESOLVED must identify the same critical alert instance")
    return records, snapshots


def publish(state, destination=DESTINATION):
    records, snapshots = validate(state)
    directory = env.directory(state["run_id"])
    destination.mkdir(parents=True, exist_ok=True)
    # Only fixed selected PNG paths and whitelisted metadata are exported.
    for record in records:
        shutil.copyfile(directory / record["file"], destination / Path(record["file"]).name)
    phases = []
    for phase, snapshot in snapshots.items():
        forecast = snapshot["responses"]["forecast"]
        body = forecast["body"]
        phases.append(
            {
                "phase": phase,
                "captured_at": snapshot["captured_at"],
                "forecast_http": forecast["status"],
                "forecast_status": body["status"],
                "readiness_http": snapshot["responses"]["ready"]["status"],
                "liveness_http": snapshot["responses"]["live"]["status"],
                "kubernetes_ready": snapshot["kubernetes_ready"],
                "prometheus_up": [
                    float(row["value"][1]) for row in snapshot.get("metrics", {}).get("up", [])
                ],
                "completed_notification_requests": snapshot.get("notifications", {}).get(
                    "successful"
                ),
                "sources": {
                    source: {
                        key: body[source][key]
                        for key in [
                            "freshness",
                            "last_success_at",
                            "cache_age_seconds",
                            "available_hours",
                            "last_outcome",
                        ]
                    }
                    for source in ["weather", "marine"]
                },
                "firing": sorted({item["metric"]["alertname"] for item in snapshot["firing"]}),
            }
        )
    manifest = {
        "run_id": state["run_id"],
        "namespace": state["namespace"],
        "started_at": state["created_at"],
        "finished_at": state["finished_at"],
        "application_image": state["image"],
        "timings": state["timings"],
        "notification_channel": "simulated_telegram_api",
        "real_telegram": "pending_user_configuration",
        "real_workloads_preserved": True,
        "cleanup": "passed",
        "rules_activated_at": json.loads((directory / "rule-activation.json").read_text())[
            "activated_at"
        ],
        "startup_notifications": 0,
        "phases": phases,
        "critical_receipts": [
            critical_receipt(state, snapshots[phase], status)
            for phase, status in [("unavailable", "FIRING"), ("recovered", "RESOLVED")]
        ],
        "screenshots": [{**record, "file": Path(record["file"]).name} for record in records],
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    local_start = datetime.fromisoformat(state["created_at"]).astimezone(
        ZoneInfo("America/Costa_Rica")
    )
    text = f"""# Failure and recovery evidence

Run **{state["run_id"]}**, {local_start:%Y-%m-%d %H:%M} America/Costa_Rica.
Namespace: `{state["namespace"]}`. All twelve captures below come from this same run.

**SIMULATED DELIVERY:** the notifications were sent by the isolated Alertmanager to a
local Telegram Bot API mock. Real private-chat delivery remains pending bot configuration.

The application, provider mock, Prometheus, Alertmanager, receiver and Grafana ran in one
temporary namespace. Provider URLs remained unchanged. Production workloads, images,
settings, dashboards and persistent volumes were preserved, and demo resources were removed.

## Observed stages

| Phase | API HTTP | Status | Ready HTTP | Weather h | Marine h | Marine age (s) | Firing alerts |
| --- | --- | --- | --- | --- | --- | --- | --- |
"""
    for row in phases:
        cells = [
            row["phase"],
            row["forecast_http"],
            row["forecast_status"],
            row["readiness_http"],
            row["sources"]["weather"]["available_hours"],
            row["sources"]["marine"]["available_hours"],
            f"{row['sources']['marine']['cache_age_seconds']:.1f}",
            ", ".join(row["firing"]) or "None",
        ]
        text += "| " + " | ".join(map(str, cells)) + " |\n"
    text += f"""
The marine download remained `{state["cached_download_at"]}` during cached failure,
while refresh attempts failed and its age grew. Weather remained usable through partial
availability. At total loss, forecasts/readiness returned 503 while liveness/metrics
continued and the exporter stayed UP. Both sources recovered and the critical condition
produced a correlated RESOLVED notification.

The table lists Prometheus firing conditions. Inhibited warnings remain FIRING there
while Alertmanager suppresses their delivery in favor of the critical condition.

Demo timings: refresh 1s, stale 15s, expiry 120s; scrape 5s, evaluation 1s, pending 5s.
These are demonstration settings, not changes to the real deployment's normal timings.
Grafana history captures use a fixed time range spanning this run. The application's
captures were taken with JavaScript disabled; Prometheus, Alertmanager and Grafana use
their actual web interfaces. Notification captures render recorded mock receipts.

## Capture catalog

"""
    titles = {
        "01-healthy-app.png": "Healthy application",
        "02-cached-app.png": "Marine failure with a usable cached forecast",
        "03-partial-app.png": "Partial availability after marine cache expiration",
        "04-unavailable-app.png": "Both sources unavailable; HTML controls remain available",
        "05-recovered-app.png": "Recovered application",
        "06-traffic-history.png": "Traffic, errors and latency across the run",
        "07-forecast-history.png": "Refresh results, age and forecast hours across the run",
        "08-prometheus-firing.png": "Firing repeated-refresh warning in Prometheus",
        "09-alertmanager-firing.png": "Critical forecast-loss alert in Alertmanager",
        "10-alertmanager-resolved.png": "Alertmanager after conditions cleared",
        "11-simulated-firing.png": "Recorded FIRING receipt — SIMULATED DELIVERY",
        "12-simulated-resolved.png": "Recorded RESOLVED receipt — SIMULATED DELIVERY",
    }
    for record in sorted(records, key=lambda r: r["file"]):
        filename = Path(record["file"]).name
        text += (
            f"### {titles[filename]}\n\n"
            f"Run `{state['run_id']}`; phase `{record['phase']}`; "
            f"captured `{record['captured_at']}` (UTC).\n\n"
            f"![{titles[filename]}]({filename})\n\n"
        )
    text += """## Reproduce

From the repository root, after the existing Kubernetes setup and Playwright installation:

```sh
python3 scripts/failure_demo.py run
python3 scripts/failure_demo.py publish-evidence --run-id <ID printed by run>
```

See [exact automatic and manual instructions](../../failure-recovery.md).
`manifest.json` records sanitized observations and SHA256 hashes for every PNG.
Full responses, queries and simulated message receipts remain in the ignored per-run artifacts.
"""
    (destination / "README.md").write_text(text)
    return str(destination / "README.md")
