"""Offline PromQL regression cases using the exact deployed Prometheus version."""

import json
import tempfile
from pathlib import Path

import prometheus

import k8s


def run(documents, interpolate):
    traffic, forecasts = {doc["uid"]: doc for doc in documents}.values()
    if traffic["uid"] != "cahuita-application":
        traffic, forecasts = forecasts, traffic

    def expression(doc, panel_id, **variables):
        panel = next(panel for panel in doc["panels"] if panel["id"] == panel_id)
        return interpolate(panel["targets"][0]["expr"], **variables)

    labels = (
        'job="cahuita",namespace="cahuita-conditions",pod="demo",container="app",'
        'instance="10.0.0.1:8000"'
    )
    target_up = "up{" + labels + "}"
    http = "http_requests_total{" + labels + ',method="GET",route="/",status_code="200"}'
    forecast_labels = "{" + labels + ',location_id="cahuita",source="weather"}'
    usable = "forecast_data_usable" + forecast_labels
    age = "forecast_cache_age_seconds" + forecast_labels
    hours = "forecast_available_hours" + forecast_labels
    last = "forecast_last_success_timestamp_seconds" + forecast_labels
    cases = []

    def case(name, expr, series, expected):
        cases.append(
            {
                "name": name,
                "interval": "15s",
                "input_series": [{"series": key, "values": value} for key, value in series],
                "promql_expr_test": [{"expr": expr, "eval_time": "2m", "exp_samples": expected}],
            }
        )

    def sample(labels, value):
        return [{"labels": labels, "value": value}]

    case(
        "no requests has no error percentage",
        expression(traffic, 5),
        [(target_up, "1+0x8"), (http, "0+0x8")],
        [],
    )
    case(
        "known traffic and no error series is zero percent",
        expression(traffic, 5),
        [(target_up, "1+0x8"), (http, "0+1x8")],
        sample("{}", 0),
    )
    case("no series is not synthesized as healthy zero", expression(traffic, 5), [], [])
    case(
        "DOWN exporter suppresses old request rates",
        expression(traffic, 2),
        [(target_up, "0+0x8"), (http, "0+1x8")],
        [],
    )
    case(
        "counter reset is accounted for before aggregation",
        expression(traffic, 3, duration="2m"),
        [(http, "0 10 20 30 40 0 10 20 30")],
        sample("{}", 68.57142857142857),
    )
    case(
        "initial download age remains absent",
        expression(forecasts, 4),
        [(target_up, "1+0x8"), (usable, "0+0x8")],
        [],
    )
    case(
        "initial lack of usable data remains a real zero",
        expression(forecasts, 2),
        [(target_up, "1+0x8"), (usable, "0+0x8")],
        sample(usable, 0),
    )
    case(
        "fresh download age",
        expression(forecasts, 4),
        [(target_up, "1+0x8"), (age, "60+0x8")],
        sample(age, 60),
    )
    case(
        "stale download age is not clamped",
        expression(forecasts, 4),
        [(target_up, "1+0x8"), (age, "1801+0x8")],
        sample(age, 1801),
    )
    case(
        "expired coverage is a real zero",
        expression(forecasts, 3),
        [(target_up, "1+0x8"), (hours, "0+0x8")],
        sample(hours, 0),
    )
    case(
        "DOWN exporter suppresses previously usable data",
        expression(forecasts, 2),
        [(target_up, "0+0x8"), (usable, "1+0x8")],
        [],
    )
    case(
        "DOWN exporter suppresses old download age",
        expression(forecasts, 4),
        [(target_up, "0+0x8"), (age, "10+0x8")],
        [],
    )
    case(
        "last download converts seconds to milliseconds after gating",
        expression(forecasts, 5),
        [(target_up, "1+0x8"), (last, "1700000000+0x8")],
        sample(forecast_labels, 1700000000000),
    )

    (k8s.ROOT / "artifacts").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="grafana-promql-", dir=k8s.ROOT / "artifacts") as temp:
        path = Path(temp) / "tests.json"
        path.write_text(
            json.dumps({"rule_files": [], "evaluation_interval": "15s", "tests": cases})
        )
        output = k8s.run(
            [
                "docker",
                "--context",
                k8s.CONTEXT,
                "run",
                "--rm",
                "--network=none",
                "--entrypoint=/bin/promtool",
                "--mount",
                f"type=bind,source={path},target=/tests.json,readonly",
                prometheus.IMAGE,
                "test",
                "rules",
                "/tests.json",
            ],
            capture=True,
        )
    (k8s.ROOT / "artifacts/grafana-promql-tests.json").write_text(
        json.dumps(cases, indent=2) + "\n"
    )
    return {"cases": len(cases), "output": output.strip()}
