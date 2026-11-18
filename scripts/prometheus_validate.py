#!/usr/bin/env python3
"""Exercise Prometheus collection, isolated provider failures, discovery and TSDB restart.

Invoked by scripts/prometheus.py validate. Only the generated fixture namespace is deleted.
"""

import json
import subprocess
import time
import uuid
from datetime import UTC, datetime

import k8s_validate as fixture
import prometheus as prom

import k8s

REPORT = k8s.ROOT / "artifacts/prometheus-validation.json"


def selector(namespace, **labels):
    labels = {"job": "cahuita", "namespace": namespace, **labels}
    return "{" + ",".join(f"{key}={json.dumps(value)}" for key, value in labels.items()) + "}"


def number(client, expression, default=None):
    result = client.query(expression)
    return float(result[0]["value"][1]) if len(result) == 1 else default


def source_values(client, metric, namespace):
    return {
        item["metric"]["source"]: float(item["value"][1])
        for item in client.query(metric + selector(namespace))
    }


def all_up(client, namespaces):
    targets = client.targets()
    own = [target for target in targets if target["labels"]["job"] == "prometheus"]
    app = [target for target in targets if target["labels"]["job"] == "cahuita"]
    if len(own) != 1 or own[0]["health"] != "up":
        return None
    if len(app) != len(namespaces) or {t["labels"]["namespace"] for t in app} != set(namespaces):
        return None
    return targets if all(target["health"] == "up" for target in app) else None


def app_identity():
    deployment = k8s.kube_json("get", "deployment", "cahuita-conditions", namespace=k8s.NAMESPACE)
    pvc = k8s.kube_json("get", "pvc", "cahuita-cache", namespace=k8s.NAMESPACE)
    pod = fixture.one_pod(k8s.NAMESPACE, require_ready=True)
    return {
        "deployment_spec": deployment["spec"],
        "pvc_uid": pvc["metadata"]["uid"],
        "pod_uid": pod["metadata"]["uid"],
        "image": pod["spec"]["containers"][0]["image"],
    }


def deployment_checks(client):
    pod = fixture.one_pod(prom.NAMESPACE, require_ready=True, label=prom.LABEL)
    pvc = k8s.kube_json("get", "pvc", "prometheus-data", namespace=prom.NAMESPACE)
    assert pvc["status"]["phase"] == "Bound"
    security = pod["spec"]["securityContext"]
    assert (security["runAsUser"], security["runAsGroup"], security["fsGroup"]) == (65534,) * 3
    runtime = k8s.kube(
        "exec", pod["metadata"]["name"], "--", "id", namespace=prom.NAMESPACE, capture=True
    ).strip()
    assert "uid=65534" in runtime and "gid=65534" in runtime
    k8s.kube(
        "exec",
        pod["metadata"]["name"],
        "--",
        "sh",
        "-ec",
        "touch /prometheus/.validation-write-test && rm /prometheus/.validation-write-test",
        namespace=prom.NAMESPACE,
    )
    architecture = k8s.kube(
        "exec", pod["metadata"]["name"], "--", "uname", "-m", namespace=prom.NAMESPACE, capture=True
    ).strip()
    assert architecture == "aarch64"
    flags = client.get("/api/v1/status/flags")
    assert flags["web.enable-admin-api"] == "false" and flags["web.enable-lifecycle"] == "false"
    permissions = {}
    for verb, resource, namespace, expected in [
        ("get", "pods", k8s.NAMESPACE, "yes"),
        ("list", "pods", k8s.NAMESPACE, "yes"),
        ("watch", "pods", k8s.NAMESPACE, "yes"),
        ("get", "secrets", k8s.NAMESPACE, "no"),
        ("patch", "pods", k8s.NAMESPACE, "no"),
        ("list", "pods", "kube-system", "no"),
    ]:
        answer = k8s.kube(
            "auth",
            "can-i",
            verb,
            resource,
            "--as=system:serviceaccount:monitoring:prometheus",
            namespace=namespace,
            capture=True,
            check=False,
        ).strip()
        assert answer == expected, (verb, resource, namespace, answer)
        permissions[f"{verb} {resource} in {namespace}"] = answer
    return {
        "pod": pod["metadata"]["name"],
        "pvc_uid": pvc["metadata"]["uid"],
        "pvc_phase": pvc["status"]["phase"],
        "runtime": runtime,
        "architecture": architecture,
        "resources": pod["spec"]["containers"][0]["resources"],
        "permissions": permissions,
        "flags": {
            key: flags[key]
            for key in [
                "storage.tsdb.retention.time",
                "storage.tsdb.retention.size",
                "web.enable-admin-api",
                "web.enable-lifecycle",
            ]
        },
    }


def request_checks(client):
    route = "/api/v1/locations/{location_id}/forecast"
    base = selector(k8s.NAMESPACE, method="GET", route=route)
    queries = {
        "ok": "sum(http_requests_total"
        + selector(k8s.NAMESPACE, method="GET", route=route, status_code="200")
        + ")",
        "not_found": "sum(http_requests_total"
        + selector(k8s.NAMESPACE, method="GET", route=route, status_code="404")
        + ")",
        "count": "sum(http_request_duration_seconds_count" + base + ")",
        "sum": "sum(http_request_duration_seconds_sum" + base + ")",
        "bucket": "sum(http_request_duration_seconds_bucket"
        + selector(k8s.NAMESPACE, method="GET", route=route, le="+Inf")
        + ")",
    }
    before = {name: number(client, query, 0) for name, query in queries.items()}
    codes = fixture.execute(
        k8s.NAMESPACE,
        """
import json,urllib.request,urllib.error
codes=[]
for location in ['cahuita']*4+['does-not-exist']*2:
    try:
        response=urllib.request.urlopen(
            'http://127.0.0.1:8000/api/v1/locations/'+location+'/forecast',timeout=5)
    except urllib.error.HTTPError as error:
        response=error
    with response:codes.append(response.status)
print(json.dumps(codes))
""",
    )
    assert codes == [200] * 4 + [404] * 2, codes

    def increased():
        after = {name: number(client, query, 0) for name, query in queries.items()}
        differences = {name: after[name] - before[name] for name in before}
        if (
            differences["ok"] >= 4
            and differences["not_found"] >= 2
            and differences["count"] >= 6
            and differences["bucket"] >= 6
            and differences["sum"] > 0
        ):
            return {"before": before, "after": after, "increase": differences, "http_codes": codes}
        return None

    result = fixture.wait_for("request counters and duration histogram", increased, timeout=25)
    rate_query = "sum(rate(http_requests_total" + base + "[2m]))"
    result["request_rate"] = fixture.wait_for(
        "request rate", lambda: number(client, rate_query, 0), timeout=25
    )
    # Exercise all required metric families against the real application.
    for metric in [
        "up",
        "http_request_duration_seconds_bucket",
        "upstream_requests_total",
        "upstream_request_duration_seconds_count",
        "forecast_refresh_total",
        "forecast_cache_age_seconds",
        "forecast_last_success_timestamp_seconds",
        "forecast_data_usable",
        "forecast_available_hours",
    ]:
        assert client.query(metric + selector(k8s.NAMESPACE)), metric
    assert client.query(
        "time() - forecast_last_success_timestamp_seconds" + selector(k8s.NAMESPACE)
    )
    return result


def failure_checks(client, namespace):
    selected = selector(namespace)
    initial_age = source_values(client, "forecast_cache_age_seconds", namespace)
    initial_upstream = client.query(
        "sum by (source, outcome) (upstream_requests_total" + selected + ")"
    )
    fixture.set_mock_mode(namespace, "error", "error")
    error_query = (
        "sum by (source) (upstream_requests_total" + selector(namespace, outcome="http_error") + ")"
    )
    failure_baseline = {
        item["metric"]["source"]: float(item["value"][1]) for item in client.query(error_query)
    }

    def failed():
        failures = {
            item["metric"]["source"]: float(item["value"][1]) for item in client.query(error_query)
        }
        return (
            failures
            if len(failures) == 2
            and all(
                value >= failure_baseline.get(source, 0) + 2 for source, value in failures.items()
            )
            else None
        )

    failures = fixture.wait_for("both failed upstream counters", failed, timeout=25)

    def aging():
        values = source_values(client, "forecast_cache_age_seconds", namespace)
        return values if len(values) == 2 and min(values.values()) >= 12 else None

    stale_age = fixture.wait_for("cache age greater than ten seconds", aging, timeout=30)
    assert all(stale_age[key] > initial_age[key] for key in initial_age)

    def expired():
        usable = source_values(client, "forecast_data_usable", namespace)
        hours = source_values(client, "forecast_available_hours", namespace)
        return (
            {"usable": usable, "hours": hours}
            if (len(usable) == 2 and set(usable.values()) == {0} and set(hours.values()) == {0})
            else None
        )

    unavailable = fixture.wait_for("expired metrics", expired, timeout=65)
    fixture.wait_for("readiness failure", lambda: not fixture.ready(namespace), timeout=20)

    def unready():
        pod = fixture.one_pod(namespace)
        return (
            pod
            if any(
                c["type"] == "Ready" and c["status"] == "False"
                for c in pod["status"].get("conditions", [])
            )
            else None
        )

    unready_pod = fixture.wait_for("Kubernetes NotReady", unready, timeout=20)
    unready_at = time.time()
    fixture.wait_for(
        "a new successful scrape while NotReady",
        lambda: number(client, "timestamp(up" + selected + ")", 0) > unready_at,
        timeout=15,
    )
    assert number(client, "up" + selected) == 1
    assert number(client, "scrape_samples_scraped" + selected, 0) > 0
    forecast_code = fixture.forecast(namespace)["status"]
    live_code = fixture.pod_http(namespace, "/health/live")["status"]
    ready_code = fixture.pod_http(namespace, "/health/ready")["status"]
    assert (forecast_code, live_code, ready_code) == (503, 200, 503)
    assert all(c["restartCount"] == 0 for c in unready_pod["status"]["containerStatuses"])
    fixture.set_mock_mode(namespace, "ok", "error")
    fixture.wait_for(
        "partial availability in Prometheus",
        lambda: (
            source_values(client, "forecast_data_usable", namespace) == {"weather": 1, "marine": 0}
        ),
        timeout=25,
    )
    fixture.wait_for("partial readiness", lambda: fixture.ready(namespace), timeout=15)
    assert fixture.forecast(namespace)["body"]["status"] == "degraded"
    partial = source_values(client, "forecast_available_hours", namespace)
    assert partial["weather"] > 0 and partial["marine"] == 0
    fixture.set_mock_mode(namespace, "ok", "ok")

    def recovered():
        usable = source_values(client, "forecast_data_usable", namespace)
        hours = source_values(client, "forecast_available_hours", namespace)
        age = source_values(client, "forecast_cache_age_seconds", namespace)
        return (
            {"usable": usable, "hours": hours, "age": age}
            if (
                len(usable) == 2
                and set(usable.values()) == {1}
                and len(hours) == 2
                and min(hours.values()) > 0
                and len(age) == 2
                and max(age.values()) < 5
            )
            else None
        )

    recovery = fixture.wait_for("both sources recovered in Prometheus", recovered, timeout=25)
    assert fixture.forecast(namespace)["body"]["status"] == "ok"
    return {
        "initial_age": initial_age,
        "initial_upstream": initial_upstream,
        "failed_attempts": failures,
        "stale_age": stale_age,
        "expired": unavailable,
        "while_not_ready": {
            "up": 1,
            "forecast_http": forecast_code,
            "liveness_http": live_code,
            "readiness_http": ready_code,
            "restarts": 0,
        },
        "partial_available_hours": partial,
        "recovered": recovery,
    }


def discovery_checks(client, namespace):
    old = fixture.one_pod(namespace, require_ready=True)
    old_name = old["metadata"]["name"]
    k8s.kube("delete", "pod", old_name, "--wait=true", namespace=namespace)

    def replaced():
        pod = fixture.one_pod(namespace, require_ready=True)
        return pod if pod["metadata"]["uid"] != old["metadata"]["uid"] else None

    new = fixture.wait_for("new temporary application pod", replaced, timeout=90)

    def discovered():
        targets = [t for t in client.targets() if t["labels"].get("namespace") == namespace]
        if (
            len(targets) == 1
            and targets[0]["labels"]["pod"] == new["metadata"]["name"]
            and targets[0]["health"] == "up"
        ):
            return targets[0]
        return None

    target = fixture.wait_for("automatic target replacement", discovered, timeout=35)
    assert number(client, "up" + selector(namespace, pod=new["metadata"]["name"])) == 1
    return {
        "old_pod": old_name,
        "new_pod": new["metadata"]["name"],
        "old_ip": old["status"]["podIP"],
        "new_ip": new["status"]["podIP"],
        "target": target,
    }


def validate():
    report = {"started_at": datetime.now(UTC).isoformat(), "context": k8s.CONTEXT, "queries": []}
    namespace = "cahuita-prom-validation-" + uuid.uuid4().hex[:10]
    created, extended = False, False
    original = app_identity()
    REPORT.parent.mkdir(exist_ok=True)
    (REPORT.parent / "prometheus-config.yml").write_text(prom.configuration())
    (REPORT.parent / "prometheus-manifests.yaml").write_text(prom.render())
    try:
        with prom.forward() as client:
            report["initial_targets"] = fixture.wait_for(
                "normal targets UP", lambda: all_up(client, [k8s.NAMESPACE])
            )
            report["deployment"] = deployment_checks(client)
            report["requests"] = request_checks(client)
            report["queries"].extend(client.queries)
        print("PASS: real targets, storage, permissions, requests and histograms", flush=True)

        k8s.kube("create", "namespace", namespace)
        created = True
        k8s.kube(
            "apply", "-f", "-", input=json.dumps(fixture.mock_objects(namespace, original["image"]))
        )
        k8s.kube(
            "rollout", "status", "deployment/mock-provider", "--timeout=120s", namespace=namespace
        )
        k8s.apply(
            original["image"],
            namespace,
            settings={
                "CAHUITA_WEATHER_BASE_URL": "http://mock-provider:9000",
                "CAHUITA_MARINE_BASE_URL": "http://mock-provider:9000",
                "CAHUITA_REFRESH_SECONDS": "1",
                "CAHUITA_STALE_SECONDS": "10",
                "CAHUITA_EXPIRE_SECONDS": "45",
                "CAHUITA_REQUEST_TIMEOUT_SECONDS": "1",
                "CAHUITA_RETRY_DELAY_SECONDS": "0",
            },
        )
        k8s.rollout(namespace)
        (REPORT.parent / "prometheus-validation-config.yml").write_text(
            prom.configuration(namespace)
        )
        extended = True  # Restore production config even if apply/rollout fails partway through.
        prom.deploy(namespace)
        with prom.forward() as client:
            fixture.wait_for(
                "real and fixture targets UP", lambda: all_up(client, [k8s.NAMESPACE, namespace])
            )
            report["failures"] = failure_checks(client, namespace)
            print(
                "PASS: upstream errors, cache aging/expiry, NotReady scraping and recovery",
                flush=True,
            )
            report["discovery"] = discovery_checks(client, namespace)
            print("PASS: automatic discovery of the replacement application pod", flush=True)
            evaluation_time = time.time()
            expression = "http_requests_total" + selector(
                k8s.NAMESPACE,
                method="GET",
                route="/api/v1/locations/{location_id}/forecast",
                status_code="200",
            )
            historical = client.query(expression, evaluation_time)
            historical_timestamp = client.query("timestamp(" + expression + ")", evaluation_time)
            assert historical and historical_timestamp
            report["queries"].extend(client.queries)
        old_prometheus = fixture.one_pod(prom.NAMESPACE, require_ready=True, label=prom.LABEL)
        k8s.kube(
            "delete",
            "pod",
            old_prometheus["metadata"]["name"],
            "--wait=true",
            namespace=prom.NAMESPACE,
        )
        fixture.wait_for(
            "replacement Prometheus Ready",
            lambda: fixture.one_pod(prom.NAMESPACE, require_ready=True, label=prom.LABEL),
            timeout=120,
        )
        with prom.forward() as client:
            fixture.wait_for(
                "targets UP after TSDB restart", lambda: all_up(client, [k8s.NAMESPACE, namespace])
            )
            after = client.query(expression, evaluation_time)
            after_timestamp = client.query("timestamp(" + expression + ")", evaluation_time)
            assert after == historical and after_timestamp == historical_timestamp
            pvc = k8s.kube_json("get", "pvc", "prometheus-data", namespace=prom.NAMESPACE)
            assert pvc["metadata"]["uid"] == report["deployment"]["pvc_uid"]
            new_prometheus = fixture.one_pod(prom.NAMESPACE, require_ready=True, label=prom.LABEL)
            assert new_prometheus["metadata"]["uid"] != old_prometheus["metadata"]["uid"]
            report["persistence"] = {
                "evaluation_time": evaluation_time,
                "query": expression,
                "before": historical,
                "after": after,
                "sample_timestamp_before": historical_timestamp,
                "sample_timestamp_after": after_timestamp,
                "pvc_uid": pvc["metadata"]["uid"],
                "old_pod": old_prometheus["metadata"]["name"],
                "new_pod": new_prometheus["metadata"]["name"],
            }
            report["queries"].extend(client.queries)
        print(
            "PASS: historical samples survive Prometheus pod replacement "
            "at a fixed evaluation time",
            flush=True,
        )
        report["result"] = "passed"
    except Exception as exc:
        report["result"], report["error"] = "failed", repr(exc)
        prom.diagnostics()
        if created:
            k8s.diagnostics(namespace)
        raise
    finally:
        try:
            if extended:
                prom.deploy()
                configmaps = k8s.kube_json("get", "configmaps", namespace=prom.NAMESPACE)["items"]
                for configmap in configmaps:
                    if configmap.get("data", {}).get("prometheus.yml") == prom.configuration(
                        namespace
                    ):
                        k8s.kube(
                            "delete",
                            "configmap",
                            configmap["metadata"]["name"],
                            namespace=prom.NAMESPACE,
                        )
            if created:
                k8s.kube("delete", "namespace", namespace, "--timeout=90s")
            with prom.forward() as client:
                report["final_targets"] = fixture.wait_for(
                    "normal targets restored", lambda: all_up(client, [k8s.NAMESPACE])
                )
                report["final_discovery_config"] = client.get("/api/v1/status/config")["yaml"]
                assert namespace not in report["final_discovery_config"]
                if "persistence" in report:
                    assert client.query(expression, evaluation_time) == historical
                    report["persistence"]["verified_after_normal_config_restore"] = True
                report["queries"].extend(client.queries)
            assert app_identity() == original, "Real application deployment, pod or PVC changed"
            report["real_application_unchanged"] = original
            print(
                "PASS: temporary resources removed; real application pod, image, "
                "settings and PVC unchanged",
                flush=True,
            )
        except Exception as exc:
            report["result"], report["cleanup_error"] = "failed", repr(exc)
            raise
        finally:
            report["finished_at"] = datetime.now(UTC).isoformat()
            REPORT.write_text(json.dumps(report, indent=2) + "\n")
            print(f"Evidence saved to {REPORT}", flush=True)


if __name__ == "__main__":
    try:
        validate()
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.CalledProcessError,
        AssertionError,
    ) as exc:
        raise SystemExit(f"Prometheus validation failed: {exc}") from exc
