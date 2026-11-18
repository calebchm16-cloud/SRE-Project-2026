#!/usr/bin/env python3
"""One continuous, isolated failure/recovery demonstration with publishable evidence."""

import argparse
import fcntl
import json
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import alertmanager as am
import alertmanager_validate as alerts
import failure_demo_env as env
import grafana
import k8s_validate as fixture
import prometheus as prom

import k8s


def now():
    return datetime.now(UTC).isoformat()


@contextmanager
def lock(state):
    with (env.directory(state["run_id"]) / ".lock").open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another phase/capture/cleanup is running for this run ID") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def firing(pc, name=None):
    selector = 'alertstate="firing"' + (f',alertname="{name}"' if name else "")
    return pc.query("ALERTS{" + selector + "}")


def observed_phase(forecast):
    if forecast["status"] == 503:
        return "unavailable"
    body = forecast["body"]
    if body["status"] == "ok":
        return "healthy"
    if (
        body["marine"]["freshness"] == "unavailable"
        and body["weather"]["freshness"] != "unavailable"
    ):
        return "partial"
    if body["marine"]["last_outcome"] != "success" and body["marine"]["hourly"]:
        return "cached"
    return "transitioning"


def snapshot(state, urls, phase):
    pc, ac = prom.Client(urls["prometheus"]), am.Client(urls["alertmanager"])
    paths = {
        "forecast": "/api/v1/locations/cahuita/forecast",
        "status": "/api/v1/status",
        "live": "/health/live",
        "ready": "/health/ready",
        "metrics": "/metrics",
    }
    responses = {key: env.http(urls["app"] + path) for key, path in paths.items()}
    # Known traffic produces meaningful route/error/histogram observations in this run.
    responses["unknown_location"] = env.http(
        urls["app"] + "/api/v1/locations/unknown-demo/forecast"
    )
    assert responses["unknown_location"]["status"] == 404
    assert responses["live"]["status"] == 200 and responses["metrics"]["status"] == 200
    current = observed_phase(responses["forecast"])
    expected = "healthy" if phase == "recovered" else phase
    if current != expected:
        raise RuntimeError(
            f"Cannot label {current} as {phase}; cache time continues during manual pauses"
        )
    namespace = state["namespace"]
    selected = f'{{job="cahuita",namespace="{namespace}"}}'
    values = {}
    for metric in [
        "up",
        "forecast_data_usable",
        "forecast_available_hours",
        "forecast_cache_age_seconds",
        "forecast_last_success_timestamp_seconds",
        "forecast_refresh_total",
        "upstream_requests_total",
        "http_requests_total",
        "http_request_duration_seconds_count",
    ]:
        values[metric] = pc.query(metric + selected)
    assert values["up"] and all(float(row["value"][1]) == 1 for row in values["up"])
    for metric in ("forecast_data_usable", "forecast_available_hours"):
        assert {
            (row["metric"].get("location_id"), row["metric"].get("source"))
            for row in values[metric]
        } == {("cahuita", "weather"), ("cahuita", "marine")}, metric
    pod = fixture.one_pod(namespace)
    messages = alerts.mock_events(namespace)
    delivered = sum(event["code"] == 200 for event in messages)
    notifications = fixture.wait_for(
        "completed simulated delivery",
        lambda: counters if (counters := ac.notifications())["successful"] >= delivered else None,
        timeout=15,
    )
    result = {
        "run_id": state["run_id"],
        "namespace": namespace,
        "phase": phase,
        "captured_at": now(),
        "responses": responses,
        "metrics": values,
        "firing": firing(pc),
        "alertmanager": ac.request("/api/v2/alerts"),
        "notifications": notifications,
        "messages": messages,
        "queries": pc.queries,
        "kubernetes_ready": any(
            c["type"] == "Ready" and c["status"] == "True"
            for c in pod["status"].get("conditions", [])
        ),
        "pod_uid": pod["metadata"]["uid"],
        "provider_urls": state["provider_urls"],
        "notification_channel": "simulated_telegram_api",
        "effective_prometheus_config": pc.get("/api/v1/status/config"),
        "effective_rules": pc.get("/api/v1/rules"),
        "effective_alertmanager_config": ac.request("/api/v2/status")["config"]["original"],
    }
    if phase in {"cached", "partial", "unavailable"}:
        assert (
            responses["forecast"]["body"]["marine"]["last_success_at"]
            == state["cached_download_at"]
        )
    if phase == "cached":
        assert (
            responses["forecast"]["body"]["marine"]["last_success_at"]
            == state["cached_download_at"]
        )
        assert any(
            row["metric"].get("source") == "marine" and float(row["value"][1]) >= 3
            for row in values["forecast_refresh_total"]
            if row["metric"].get("outcome") != "success"
        )
        assert firing(pc, "CahuitaRepeatedRefreshFailures")
    elif phase == "partial":
        assert responses["forecast"]["body"]["marine"]["available_hours"] == 0
        assert responses["forecast"]["body"]["weather"]["available_hours"] > 0
        assert responses["ready"]["status"] == 200
    elif phase == "unavailable":
        assert responses["ready"]["status"] == 503 and result["kubernetes_ready"] is False
        assert all(
            float(row["value"][1]) == 0
            for metric in ("forecast_data_usable", "forecast_available_hours")
            for row in values[metric]
        )
        assert firing(pc, "CahuitaForecastsUnavailable")
    elif phase in {"healthy", "recovered"}:
        assert responses["ready"]["status"] == 200 and result["kubernetes_ready"] is True
        assert not result["firing"]
        assert all(
            responses["forecast"]["body"][source]["available_hours"] == 48
            for source in ("weather", "marine")
        )
    return result


def screenshot(state, phase, urls):
    interpreter = k8s.ROOT / ".venv/bin/python"
    if not interpreter.exists():
        raise RuntimeError("Run uv sync --frozen and install Playwright Chromium before this demo")
    # URLs are localhost, not credentials; password is read from the isolated Secret by the child.
    result = k8s.run(
        [
            interpreter,
            "scripts/failure_demo_browser.py",
            "--run-id",
            state["run_id"],
            "--phase",
            phase,
            "--urls",
            json.dumps(urls),
        ],
        capture=True,
        timeout=120,
    )
    return json.loads(result)


def capture(state, phase=None):
    env.ownership(state)
    phase = phase or state["phase"]
    with env.forwards(state) as urls:
        result = snapshot(state, urls, phase)
        path = env.directory(state["run_id"])
        (path / f"{phase}.json").write_text(json.dumps(result, indent=2) + "\n")
        images = screenshot(state, phase, urls)
        # Detect a phase expiring while the browser was taking screenshots.
        actual = observed_phase(env.http(urls["app"] + "/api/v1/locations/cahuita/forecast"))
        if actual != ("healthy" if phase == "recovered" else phase):
            raise RuntimeError(
                "The phase changed during capture; no evidence was accepted for this phase"
            )
    state["phases"][phase] = {
        "captured_at": result["captured_at"],
        "snapshot": f"{phase}.json",
        "images": images,
        "observed_phase": actual,
    }
    state["phase"] = phase
    if phase == "recovered":
        state["result"] = "passed" if list(state["phases"]) == list(env.PHASES) else "incomplete"
    env.save(state)
    print(f"Captured {phase}: {len(images)} screenshots; run {state['run_id']}", flush=True)


def wait_ready(state, urls):
    gc = grafana.Client(urls["grafana"], env.grafana_password(state))
    pc = prom.Client(urls["prometheus"])
    fixture.wait_for(
        "demo Grafana datasource",
        lambda: gc.request("/api/datasources/uid/cahuita-prometheus/health")["status"] == "OK",
        timeout=90,
    )
    successful_scrape = fixture.wait_for(
        "successful demo scrape",
        lambda: (
            rows
            if (
                (rows := pc.query(f'up{{job="cahuita",namespace="{state["namespace"]}"}}'))
                and all(float(row["value"][1]) == 1 for row in rows)
            )
            else None
        ),
    )
    sources = fixture.wait_for(
        "both usable sources in Prometheus",
        lambda: (
            rows
            if (
                len(
                    rows := pc.query(
                        f'forecast_data_usable{{job="cahuita",namespace="{state["namespace"]}"}}'
                    )
                )
                == 2
                and all(float(row["value"][1]) == 1 for row in rows)
            )
            else None
        ),
    )
    assert not pc.get("/api/v1/rules")["groups"]
    env.activate_rules(state)
    fixture.wait_for(
        "activated demo rules",
        lambda: len(pc.get("/api/v1/rules")["groups"]) == 1,
    )
    fixture.wait_for("startup without firing alerts", lambda: not firing(pc))
    assert not alerts.mock_events(state["namespace"]), "Unexpected startup notification"
    (env.directory(state["run_id"]) / "rule-activation.json").write_text(
        json.dumps(
            {
                "activated_at": now(),
                "up_before_activation": successful_scrape,
                "sources_before_activation": sources,
                "startup_notifications": 0,
                "reload": "SIGHUP; HTTP reload and admin APIs remain disabled",
            },
            indent=2,
        )
        + "\n"
    )


def start(mode="manual"):
    run_id = uuid.uuid4().hex[:10]
    path = env.directory(run_id)
    path.mkdir(parents=True)
    state = {
        "version": 1,
        "run_id": run_id,
        "namespace": "cahuita-alert-validation-" + run_id,
        "created_at": now(),
        "context": k8s.CONTEXT,
        "mode": mode,
        "phase": "preparing",
        "result": "incomplete",
        "phases": {},
        "notification_channel": "simulated_telegram_api",
        "real_telegram": "pending_user_configuration",
        "cleanup": "pending",
        "timings": {
            "refresh_seconds": 1,
            "stale_seconds": 15,
            "expire_seconds": 120,
            "scrape_seconds": 5,
            "evaluation_seconds": 1,
            "alert_for_seconds": 5,
        },
        "provider_urls": {
            "weather": "http://mock-provider:9000",
            "marine": "http://mock-provider:9000",
        },
        "before": env.preserved_workloads(),
    }
    state["image"] = state["before"][k8s.NAMESPACE]["deployments"]["cahuita-conditions"][
        "template"
    ]["spec"]["containers"][0]["image"]
    env.save(state)
    print(f"Starting isolated demo {run_id} in {state['namespace']}", flush=True)
    try:
        with lock(state):
            env.prepare(state)
            with env.forwards(state) as urls:
                wait_ready(state, urls)
            capture(state, "healthy")
    except BaseException:
        state["result"] = "interrupted" if sys.exc_info()[0] is KeyboardInterrupt else "failed"
        env.save(state)
        cleanup(state)
        raise
    return state


def phase(state, requested):
    with lock(state):
        env.ownership(state)
        if requested != "recovered":
            expected_previous = env.PHASES[env.PHASES.index(requested) - 1]
            if state["phase"] != expected_previous:
                raise ValueError(
                    f"Expected {expected_previous} before {requested}; "
                    "recovery/cleanup is always allowed"
                )
        namespace = state["namespace"]
        if requested == "cached":
            fixture.set_mock_mode(namespace, "ok", "error")

            def failed_cached():
                body = fixture.forecast(namespace)["body"]
                return (
                    body
                    if body["marine"]["last_outcome"] == "http_error" and body["marine"]["hourly"]
                    else None
                )

            first = fixture.wait_for("failed marine refresh with usable cache", failed_cached)
            state["cached_download_at"] = first["marine"]["last_success_at"]
            env.save(state)
            with prom.forward(namespace) as pc:
                fixture.wait_for(
                    "repeated refresh warning",
                    lambda: firing(pc, "CahuitaRepeatedRefreshFailures"),
                    timeout=60,
                )
            alerts.wait_message(namespace, "CahuitaRepeatedRefreshFailures")
        elif requested == "partial":
            with prom.forward(namespace) as pc:
                fixture.wait_for(
                    "marine cache expiry and source warning",
                    lambda: firing(pc, "CahuitaForecastSourceUnusable"),
                    timeout=150,
                )
            alerts.wait_message(namespace, "CahuitaForecastSourceUnusable")
        elif requested == "unavailable":
            fixture.set_mock_mode(namespace, "error", "error")
            with prom.forward(namespace) as pc:
                fixture.wait_for(
                    "complete cache expiry and critical alert",
                    lambda: firing(pc, "CahuitaForecastsUnavailable"),
                    timeout=160,
                )
            fixture.wait_for(
                "Kubernetes NotReady",
                lambda: any(
                    c["type"] == "Ready" and c["status"] == "False"
                    for c in fixture.one_pod(namespace)["status"].get("conditions", [])
                ),
                timeout=20,
            )
            alerts.wait_message(namespace, "CahuitaForecastsUnavailable")
        else:
            fixture.set_mock_mode(namespace, "ok", "ok")
            fixture.wait_for(
                "usable data restored",
                lambda: fixture.forecast(namespace)["body"]["status"] == "ok",
            )
            fixture.wait_for("readiness restored", lambda: fixture.ready(namespace))
            with prom.forward(namespace) as pc:
                fixture.wait_for("all alert conditions cleared", lambda: not firing(pc), timeout=45)
            if "unavailable" in state["phases"]:
                alerts.wait_message(namespace, "CahuitaForecastsUnavailable", "RESOLVED")
        capture(state, requested)


def cleanup(state):
    with lock(state):
        if state.get("cleanup") == "passed":
            print(f"Run {state['run_id']} was already cleaned; retaining its original verification")
            return
        if state.get("namespace_uid"):
            found = k8s.kube(
                "get",
                "namespace",
                state["namespace"],
                "--ignore-not-found",
                "-o",
                "name",
                capture=True,
            )
            if found.strip():
                env.ownership(state)
                k8s.kube("delete", "namespace", state["namespace"], "--timeout=120s")
        state["after"] = env.preserved_workloads()
        state["preserved"] = state["after"] == state["before"]
        state["cleanup"] = "passed"
        state["finished_at"] = now()
        if not state["preserved"]:
            state["result"] = "failed"
            env.save(state)
            raise RuntimeError(
                "Real workload specs, pod identities or PVCs changed during the demo"
            )
        env.save(state)
        print(
            f"Cleaned owned demo resources; real workloads unchanged ({state['run_id']})",
            flush=True,
        )


def run():
    state = start("automatic")
    try:
        for name in env.PHASES[1:]:
            print(f"Entering {name}", flush=True)
            phase(state, name)
    except BaseException:
        state["result"] = "interrupted" if sys.exc_info()[0] is KeyboardInterrupt else "failed"
        env.save(state)
        raise
    finally:
        cleanup(state)
    print(
        f"Complete run: {state['run_id']}. Publish with publish-evidence --run-id {state['run_id']}"
    )
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "run",
            "start",
            "phase",
            "status",
            "capture",
            "access",
            "credentials",
            "cleanup",
            "publish-evidence",
        ],
    )
    parser.add_argument("phase", nargs="?", choices=env.PHASES[1:])
    parser.add_argument("--run-id")
    args = parser.parse_args()
    if args.command == "run":
        run()
        return
    if args.command == "start":
        state = start()
        print(f"RUN_ID={state['run_id']}")
        return
    if not args.run_id:
        parser.error("--run-id is required for this command")
    state = env.load(args.run_id)
    if args.command == "phase":
        if not args.phase:
            parser.error("phase requires cached, partial, unavailable or recovered")
        phase(state, args.phase)
    elif args.command == "cleanup":
        cleanup(state)
    elif args.command == "capture":
        with lock(state):
            capture(state)
    elif args.command == "credentials":
        print(f"Demo Grafana user: admin\nDemo password: {env.grafana_password(state)}")
    elif args.command == "access":
        with env.forwards(state, fixed=True) as urls:
            print(json.dumps(urls, indent=2), flush=True)
            print("Localhost-only access. Ctrl-C closes only these forwards.", flush=True)
            while True:
                urls.check()
                time.sleep(1)
    elif args.command == "status":
        with env.forwards(state) as urls:
            observed = env.http(urls["app"] + "/api/v1/locations/cahuita/forecast")
        print(
            json.dumps(
                {
                    "run_id": state["run_id"],
                    "last_captured_phase": state["phase"],
                    "observed_now": observed_phase(observed),
                    "forecast": observed,
                },
                indent=2,
            )
        )
    else:
        from failure_demo_publish import publish

        print(publish(state))


if __name__ == "__main__":

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    try:
        main()
    except KeyboardInterrupt:
        print(
            "Interrupted. Automatic mode cleans up; manual mode: cleanup --run-id <ID>.",
            file=sys.stderr,
        )
        sys.exit(130)
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.CalledProcessError,
        AssertionError,
    ) as exc:
        sys.exit(f"Failure demo error: {exc}")
