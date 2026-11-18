"""Offline checks of the production alert expressions, labels, and pending timers."""

import json
import tempfile
from pathlib import Path

import prometheus

import k8s


def run():
    ns = "cahuita-conditions"
    labels = f'job="cahuita",namespace="{ns}",pod="demo",container="app",instance="10.0.0.1:8000"'
    up = "up{" + labels + "}"
    ready503 = (
        "http_requests_total{" + labels + ',method="GET",route="/health/ready",status_code="503"}'
    )
    ready200 = ready503.replace('"503"', '"200"')

    def forecast(source):
        return "forecast_data_usable{" + labels + f',location_id="cahuita",source="{source}"}}'

    failures = (
        "forecast_refresh_total{"
        + labels
        + ',location_id="cahuita",source="weather",outcome="http_error"}'
    )
    successes = failures.replace('"http_error"', '"success"')
    cases = []

    def expected(name, severity, **extra):
        fields = {
            "alertname": name,
            "alertstate": "firing",
            "namespace": ns,
            "service": "cahuita-conditions",
            "severity": severity,
            **extra,
        }
        return [
            {
                "labels": "ALERTS{" + ",".join(f'{k}="{v}"' for k, v in fields.items()) + "}",
                "value": 1,
            }
        ]

    def case(name, alert, series, checks):
        cases.append(
            {
                "name": name,
                "interval": "15s",
                "input_series": [{"series": key, "values": value} for key, value in series],
                "promql_expr_test": [
                    {
                        "expr": f'ALERTS{{alertname="{alert}",alertstate="firing"}}',
                        "eval_time": at,
                        "exp_samples": samples,
                    }
                    for at, samples in checks
                ],
            }
        )

    unreachable = "CahuitaApplicationUnreachable"
    notready = "CahuitaApplicationNotReady"
    repeated = "CahuitaRepeatedRefreshFailures"
    source = "CahuitaForecastSourceUnusable"
    total = "CahuitaForecastsUnavailable"
    warn_source = expected(source, "warning", location_id="cahuita", source="weather")
    case("healthy exporter", unreachable, [(up, "1+0x80")], [("10m", [])])
    case(
        "DOWN waits two minutes",
        unreachable,
        [(up, "0+0x24")],
        [("1m45s", []), ("2m", expected(unreachable, "critical"))],
    )
    case("missing target is detected", unreachable, [], [("2m", expected(unreachable, "critical"))])
    case("brief DOWN is tolerated", unreachable, [(up, "0 0 1+0x20")], [("3m", [])])
    case(
        "pending readiness then firing",
        notready,
        [(up, "1+0x40"), (ready503, "0+1x40")],
        [("2m", []), ("2m15s", expected(notready, "critical"))],
    )
    case(
        "successful readiness clears pending failure",
        notready,
        [(up, "1+0x40"), (ready503, "0+1x40"), (ready200, "0+0x4 1+1x36")],
        [("5m", [])],
    )
    case(
        "readiness is ignored without a reachable exporter",
        notready,
        [(up, "0+0x40"), (ready503, "0+1x40")],
        [("5m", [])],
    )
    case(
        "one source missing its data",
        source,
        [(up, "1+0x40"), (forecast("weather"), "0+0x40"), (forecast("marine"), "1+0x40")],
        [("4m45s", []), ("5m", warn_source)],
    )
    case(
        "a usable source prevents total loss",
        total,
        [(up, "1+0x40"), (forecast("weather"), "0+0x40"), (forecast("marine"), "1+0x40")],
        [("5m", [])],
    )
    case(
        "both sources unavailable",
        total,
        [(up, "1+0x40"), (forecast("weather"), "0+0x40"), (forecast("marine"), "0+0x40")],
        [("1m45s", []), ("2m", expected(total, "critical", location_id="cahuita"))],
    )
    case(
        "absent source is not a zero",
        total,
        [(up, "1+0x40"), (forecast("weather"), "0+0x40")],
        [("5m", [])],
    )
    case("no initial download telemetry is not fabricated", source, [(up, "1+0x40")], [("5m", [])])
    case(
        "stale data is ignored after exporter loss",
        source,
        [(up, "0+0x40"), (forecast("weather"), "0+0x40")],
        [("5m", [])],
    )
    case(
        "observed repeated refresh increments",
        repeated,
        [(up, "1+0x80"), (failures, "0+1x80")],
        [
            ("5m", []),
            ("6m", expected(repeated, "warning", location_id="cahuita", source="weather")),
        ],
    )
    case(
        "two failed increments are insufficient",
        repeated,
        [(up, "1+0x80"), (failures, "0 1 2+0x78")],
        [("10m", [])],
    )
    case(
        "successes interleaved with failures suppress repetition",
        repeated,
        [(up, "1+0x80"), (failures, "0+1x80"), (successes, "0+1x80")],
        [("10m", [])],
    )
    case(
        "reset counters still count failed operations",
        repeated,
        [(up, "1+0x80"), (failures, "0 1 2 0 1 2 2+0x74")],
        [("10m", expected(repeated, "warning", location_id="cahuita", source="weather"))],
    )
    case(
        "source recovery clears its condition",
        source,
        [(up, "1+0x40"), (forecast("weather"), "0+0x23 1+0x16")],
        [("5m30s", warn_source), ("6m", [])],
    )
    case(
        "other validation namespaces never page production",
        source,
        [(up, "1+0x40"), (forecast("weather").replace(ns, "cahuita-other-validation"), "0+0x40")],
        [("5m", [])],
    )
    case(
        "recent success clears repeated failures",
        repeated,
        [(up, "1+0x80"), (failures, "0+1x80"), (successes, "0+0x24 1+0x56")],
        [
            ("6m", expected(repeated, "warning", location_id="cahuita", source="weather")),
            ("7m", []),
        ],
    )

    output = k8s.ROOT / "artifacts/alertmanager"
    output.mkdir(parents=True, exist_ok=True)
    payload = {
        "rule_files": ["/etc/prometheus/alerts.yml"],
        "evaluation_interval": "15s",
        "tests": cases,
    }
    with tempfile.TemporaryDirectory(prefix="alert-rules-", dir=output) as temp:
        path = Path(temp)
        path.chmod(0o755)  # Nonroot promtool must traverse the mounted fixture directory.
        (path / "alerts.yml").write_text((prometheus.BASE / "alerts.yml").read_text())
        (path / "tests.json").write_text(json.dumps(payload))
        text = k8s.run(
            [
                "docker",
                "--context",
                k8s.CONTEXT,
                "run",
                "--rm",
                "--network=none",
                "--entrypoint=/bin/promtool",
                "--mount",
                f"type=bind,source={path},target=/etc/prometheus,readonly",
                prometheus.IMAGE,
                "test",
                "rules",
                "/etc/prometheus/tests.json",
            ],
            capture=True,
        )
    (output / "rule-tests.json").write_text(json.dumps(payload, indent=2) + "\n")
    return {"cases": len(cases), "result": text.strip()}
