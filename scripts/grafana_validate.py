"""Acceptance checks through Grafana, plus isolated application/provider failures."""

import json
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime

import grafana
import grafana_promql_tests
import k8s_validate as fixture
import prometheus as prom
import prometheus_validate as prior

import k8s

OUTPUT = k8s.ROOT / "artifacts/grafana"


def expression(uid, panel_id, **variables):
    doc = next(doc for doc in grafana.dashboards() if doc["uid"] == uid)
    panel = next(panel for panel in doc["panels"] if panel["id"] == panel_id)
    return grafana.interpolate(panel["targets"][0]["expr"], **variables)


def values(client, panel_id, namespace):
    return {
        item["metric"]["source"]: float(item["value"][1])
        for item in client.query(expression("cahuita-forecasts", panel_id, namespace=namespace))
    }


def browser(namespace=k8s.NAMESPACE, phase="normal"):
    interpreter = k8s.ROOT / ".venv/bin/python"
    if not interpreter.exists():
        raise RuntimeError("Browser validation requires uv sync --frozen and Playwright Chromium")
    output = k8s.run(
        [interpreter, "scripts/grafana_browser.py", "--namespace", namespace, "--phase", phase],
        capture=True,
        timeout=180,
    )
    return json.loads(output)


def provisioning(client):
    health = client.request("/api/health")
    assert health["database"] == "ok" and health["version"] == "13.3.0"
    datasource = client.request(f"/api/datasources/uid/{grafana.DATASOURCE}")
    assert datasource["readOnly"] and datasource["isDefault"]
    assert datasource["url"] == "http://prometheus.monitoring.svc.cluster.local:9090"
    assert client.request(f"/api/datasources/uid/{grafana.DATASOURCE}/health")["status"] == "OK"
    documents = {}
    for uid in grafana.DASHBOARDS:
        doc = client.request("/api/dashboards/uid/" + uid)
        assert doc["meta"]["provisioned"] and doc["meta"]["folderUid"] == "cahuita"
        assert doc["dashboard"]["editable"] is False
        documents[uid] = {
            "id": doc["dashboard"]["id"],
            "title": doc["dashboard"]["title"],
            "panels": len(doc["dashboard"]["panels"]),
        }
    try:
        urllib.request.urlopen(client.url + "/api/search", timeout=5)
    except urllib.error.HTTPError as exc:
        assert exc.code == 401
    else:
        raise AssertionError("Unauthenticated dashboard API access was allowed")
    queries = []
    for doc in grafana.dashboards():
        for panel in doc["panels"]:
            for target in panel["targets"]:
                result = client.query(grafana.interpolate(target["expr"]))
                queries.append(
                    {
                        "dashboard": doc["uid"],
                        "panel": panel["id"],
                        "refId": target["refId"],
                        "series": len(result),
                    }
                )
    # The last-success panel must return epoch milliseconds, not a 1970 date.
    last = client.query(expression("cahuita-forecasts", 5))
    assert last and min(float(item["value"][1]) for item in last) > 1e12
    return {
        "health": health,
        "datasource": {"uid": datasource["uid"], "readOnly": True},
        "dashboards": documents,
        "queries": queries,
        "anonymous_api_http": 401,
    }


def runtime():
    pod = fixture.one_pod(grafana.NAMESPACE, require_ready=True, label=grafana.LABEL)
    pvc = k8s.kube_json("get", "pvc", "grafana-data", namespace=grafana.NAMESPACE)
    assert pvc["status"]["phase"] == "Bound"
    context = pod["spec"]["securityContext"]
    assert [context[key] for key in ["runAsUser", "runAsGroup", "fsGroup"]] == [472] * 3
    assert pod["spec"]["automountServiceAccountToken"] is False
    container = pod["spec"]["containers"][0]
    assert container["resources"] == {
        "requests": {"cpu": "100m", "memory": "128Mi"},
        "limits": {"cpu": "500m", "memory": "512Mi"},
    }
    for probe in ["startupProbe", "readinessProbe", "livenessProbe"]:
        assert container[probe]["httpGet"]["path"] == "/api/health"
    output = k8s.kube(
        "exec",
        pod["metadata"]["name"],
        "--",
        "sh",
        "-ec",
        'test "$(id -u)" = 472; test "$(id -g)" = 472; '
        'test "$(uname -m)" = aarch64; '
        "printf grafana-persistent > /var/lib/grafana/.acceptance-marker; "
        "test -s /var/lib/grafana/grafana.db; id; uname -m",
        namespace=grafana.NAMESPACE,
        capture=True,
    )
    secret_uid = k8s.kube(
        "get",
        "secret",
        "grafana-admin",
        "-o",
        "jsonpath={.metadata.uid}",
        namespace=grafana.NAMESPACE,
        capture=True,
    )
    return {
        "pod": pod["metadata"]["name"],
        "pod_uid": pod["metadata"]["uid"],
        "pvc_uid": pvc["metadata"]["uid"],
        "secret_uid": secret_uid,
        "runtime": output.strip(),
        "resources": container["resources"],
    }


def failure_recovery(client, namespace, report):
    def usable():
        return values(client, 2, namespace) == {"weather": 1, "marine": 1}

    fixture.wait_for("both fixture sources in Grafana", usable, timeout=30)
    report["initial_age"] = values(client, 4, namespace)
    fixture.set_mock_mode(namespace, "error", "error")
    error_expr = (
        "sum by (source, outcome) (upstream_requests_total"
        + prior.selector(namespace, outcome="http_error")
        + ")"
    )

    def failures():
        result = client.query(error_expr)
        return (
            result if len(result) == 2 and min(float(i["value"][1]) for i in result) >= 2 else None
        )

    report["failed_attempts"] = fixture.wait_for(
        "upstream failures through Grafana", failures, timeout=30
    )

    def aging():
        age = values(client, 4, namespace)
        return age if len(age) == 2 and min(age.values()) > 12 else None

    report["aged_cache"] = fixture.wait_for(
        "increasing download age through Grafana", aging, timeout=30
    )

    def expired():
        return values(client, 2, namespace) == {"weather": 0, "marine": 0} and values(
            client, 3, namespace
        ) == {"weather": 0, "marine": 0}

    fixture.wait_for("expired forecasts through Grafana panels", expired, timeout=65)
    fixture.wait_for("fixture NotReady", lambda: not fixture.ready(namespace), timeout=20)
    fixture.wait_for(
        "Kubernetes NotReady",
        lambda: any(
            c["type"] == "Ready" and c["status"] == "False"
            for c in fixture.one_pod(namespace)["status"].get("conditions", [])
        ),
        timeout=20,
    )
    observed_at = time.time()
    fixture.wait_for(
        "fresh metrics while NotReady",
        lambda: (
            prior.number(client, "timestamp(up" + prior.selector(namespace) + ")", 0) > observed_at
        ),
        timeout=15,
    )
    assert prior.number(client, "up" + prior.selector(namespace)) == 1
    assert fixture.pod_http(namespace, "/health/live")["status"] == 200
    report["expired"] = {
        "usable": values(client, 2, namespace),
        "hours": values(client, 3, namespace),
        "up": 1,
        "readiness_http": 503,
        "liveness_http": 200,
    }
    report["expired_browser"] = browser(namespace, "expired")
    fixture.set_mock_mode(namespace, "ok", "error")
    fixture.wait_for(
        "partial recovery through Grafana",
        lambda: values(client, 2, namespace) == {"weather": 1, "marine": 0},
        timeout=25,
    )
    fixture.wait_for("partial readiness", lambda: fixture.ready(namespace), timeout=15)
    report["partial_hours"] = values(client, 3, namespace)
    fixture.set_mock_mode(namespace, "ok", "ok")
    fixture.wait_for("full recovery through Grafana", usable, timeout=25)
    report["recovered"] = {
        "usable": values(client, 2, namespace),
        "hours": values(client, 3, namespace),
        "age": values(client, 4, namespace),
    }
    assert all(v > 0 for v in report["recovered"]["hours"].values())
    assert max(report["recovered"]["age"].values()) < 5
    report["recovered_browser"] = browser(namespace, "recovered")


def validate():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    report = {"started_at": datetime.now(UTC).isoformat(), "queries": []}
    namespace = "cahuita-prom-validation-" + uuid.uuid4().hex[:10]
    original = prior.app_identity()
    created, extended = False, False
    try:
        report["static"] = grafana.check()
        report["promql"] = grafana_promql_tests.run(grafana.dashboards(), grafana.interpolate)
        report["runtime"] = runtime()
        with grafana.forward() as client:
            report["provisioning"] = provisioning(client)
            report["traffic"] = prior.request_checks(client)
            report["queries"].extend(client.queries)
        report["normal_browser"] = browser()
        print(
            "PASS: provisioning, 26 panel queries, HTTP traffic and normal browser checks",
            flush=True,
        )

        k8s.kube("create", "namespace", namespace, "--save-config")
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
        extended = True
        prom.deploy(namespace)
        with grafana.forward() as client:
            report["failures"] = {}
            failure_recovery(client, namespace, report["failures"])
            report["queries"].extend(client.queries)
        print(
            "PASS: upstream failures, aging, expiry, NotReady scrapes and recovery in Grafana",
            flush=True,
        )

        old = fixture.one_pod(grafana.NAMESPACE, require_ready=True, label=grafana.LABEL)
        k8s.kube(
            "delete", "pod", old["metadata"]["name"], "--wait=true", namespace=grafana.NAMESPACE
        )
        fixture.wait_for(
            "replacement Grafana Ready",
            lambda: fixture.one_pod(grafana.NAMESPACE, require_ready=True, label=grafana.LABEL),
            timeout=120,
        )
        with grafana.forward() as client:
            after = provisioning(client)
            assert after["dashboards"] == report["provisioning"]["dashboards"]
            new = fixture.one_pod(grafana.NAMESPACE, require_ready=True, label=grafana.LABEL)
            assert old["metadata"]["uid"] != new["metadata"]["uid"]
            assert (
                k8s.kube_json("get", "pvc", "grafana-data", namespace=grafana.NAMESPACE)[
                    "metadata"
                ]["uid"]
                == report["runtime"]["pvc_uid"]
            )
            assert (
                k8s.kube(
                    "get",
                    "secret",
                    "grafana-admin",
                    "-o",
                    "jsonpath={.metadata.uid}",
                    namespace=grafana.NAMESPACE,
                    capture=True,
                )
                == report["runtime"]["secret_uid"]
            )
            marker = k8s.kube(
                "exec",
                new["metadata"]["name"],
                "--",
                "cat",
                "/var/lib/grafana/.acceptance-marker",
                namespace=grafana.NAMESPACE,
                capture=True,
            )
            assert marker == "grafana-persistent"
            report["persistence"] = {
                "old_pod": old["metadata"]["name"],
                "new_pod": new["metadata"]["name"],
                "pvc_uid": report["runtime"]["pvc_uid"],
                "secret_uid": report["runtime"]["secret_uid"],
                "dashboard_ids_unchanged": True,
                "login_after_restart": "passed",
                "marker": marker,
            }
            report["queries"].extend(client.queries)
        print(
            "PASS: Grafana login, SQLite dashboard identities and volume survive pod replacement",
            flush=True,
        )
        report["result"] = "passed"
    except Exception as exc:
        report["result"], report["error"] = "failed", str(exc)
        grafana.diagnostics()
        raise
    finally:
        try:
            if extended:
                prom.deploy()
                for cm in k8s.kube_json("get", "configmaps", namespace=prom.NAMESPACE)["items"]:
                    if cm.get("data", {}).get("prometheus.yml") == prom.configuration(namespace):
                        k8s.kube(
                            "delete", "configmap", cm["metadata"]["name"], namespace=prom.NAMESPACE
                        )
            if created:
                k8s.kube("delete", "namespace", namespace, "--timeout=90s")
            with prom.forward() as client:
                targets = fixture.wait_for(
                    "normal targets restored", lambda: prior.all_up(client, [k8s.NAMESPACE])
                )
                report["final_targets"] = [
                    {"labels": t["labels"], "health": t["health"]} for t in targets
                ]
            assert prior.app_identity() == original, (
                "Real application pod, image, settings or PVC changed"
            )
            report["real_application_unchanged"] = original
            with grafana.forward() as client:
                report["final_health"] = client.request("/api/health")
            pod = fixture.one_pod(grafana.NAMESPACE, require_ready=True, label=grafana.LABEL)
            k8s.kube(
                "exec",
                pod["metadata"]["name"],
                "--",
                "rm",
                "-f",
                "/var/lib/grafana/.acceptance-marker",
                namespace=grafana.NAMESPACE,
            )
        except Exception as exc:
            report["result"], report["cleanup_error"] = "failed", str(exc)
            raise
        finally:
            report["finished_at"] = datetime.now(UTC).isoformat()
            (OUTPUT / "validation.json").write_text(json.dumps(report, indent=2) + "\n")
            print(f"Evidence saved to {OUTPUT / 'validation.json'}", flush=True)
