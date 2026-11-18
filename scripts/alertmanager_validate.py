"""Run isolated rule/notification acceptance; real Telegram is tested separately on request."""

import copy
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import alertmanager as am
import alertmanager_rule_tests
import k8s_validate as fixture
import prometheus as prom
import prometheus_validate as prior

import k8s

OUTPUT = k8s.ROOT / "artifacts/alertmanager"
MOCK_LABEL = "app=telegram-mock"


def synthetic(name, namespace, marker, *, source=None, location="cahuita", severity="warning"):
    now = datetime.now(UTC)
    labels = {
        "alertname": name,
        "namespace": namespace,
        "service": "cahuita-conditions",
        "severity": severity,
        "validation_case": marker,
    }
    if source:
        labels["source"] = source
    if location:
        labels["location_id"] = location
    return {
        "labels": labels,
        "startsAt": now.isoformat(),
        "endsAt": (now + timedelta(minutes=20)).isoformat(),
        "annotations": {
            "summary": marker,
            "description": "Isolated synthetic validation; no real incident.",
            "action": "No external action is required.",
        },
    }


def resolve(client, alerts):
    ended = copy.deepcopy(alerts)
    for alert in ended:
        alert["endsAt"] = datetime.now(UTC).isoformat()
    client.request("/api/v2/alerts", ended)


def mock_events(namespace):
    return fixture.execute(
        namespace,
        """
import json,urllib.request
with urllib.request.urlopen('http://127.0.0.1:9001/events',timeout=3) as response:
    print(response.read().decode())
""",
        label=MOCK_LABEL,
    )


def accepted(namespace, marker, status=None):
    return [
        event
        for event in mock_events(namespace)
        if event["code"] == 200
        and marker in event["text"]
        and (status is None or event["text"].startswith(status))
    ]


def wait_message(namespace, marker, status="FIRING", timeout=60):
    return fixture.wait_for(
        f"mock Telegram {status} / {marker}",
        lambda: accepted(namespace, marker, status),
        timeout=timeout,
    )


def setup_objects(namespace, image, output=OUTPUT):
    """Use the existing app/mock and exact Prometheus image, with isolated rules and RBAC."""
    config = prom.configuration().replace("names: [cahuita-conditions]", f"names: [{namespace}]")
    config = config.replace("evaluation_interval: 15s", "evaluation_interval: 1s")
    config = config.replace(
        "alertmanager.monitoring.svc.cluster.local", f"alertmanager.{namespace}.svc.cluster.local"
    )
    rules = (
        (prom.BASE / "alerts.yml")
        .read_text()
        .replace('namespace="cahuita-conditions"', f'namespace="{namespace}"')
    )
    rules = rules.replace("name: cahuita-application", "name: cahuita-validation\n    interval: 1s")
    rules = rules.replace("[1m]", "[10s]").replace("[1h]", "[1m]").replace("[30m]", "[10s]")
    rules = rules.replace("for: 2m", "for: 5s").replace("for: 5m", "for: 5s")
    (output / "validation-prometheus.yml").write_text(config)
    (output / "validation-alerts.yml").write_text(rules)
    mock = fixture.mock_objects(namespace, image)
    telegram = copy.deepcopy(mock["items"][-1])
    telegram["metadata"]["name"] = "telegram-mock"
    telegram["spec"]["selector"]["matchLabels"] = {"app": "telegram-mock"}
    template = telegram["spec"]["template"]
    template["metadata"]["labels"] = {"app": "telegram-mock"}
    container = template["spec"]["containers"][0]
    container["command"] = ["python", "/fixture/mock_telegram.py"]
    container["ports"] = [{"containerPort": 9001}]
    container["readinessProbe"]["tcpSocket"]["port"] = 9001
    template["spec"]["volumes"][0]["configMap"]["name"] = "telegram-mock-script"
    selector = {"app": "alert-validation-prometheus"}
    prometheus = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "prometheus", "namespace": namespace},
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": selector},
            "template": {
                "metadata": {"labels": selector},
                "spec": {
                    "serviceAccountName": "prometheus",
                    "securityContext": {"runAsUser": 65534, "runAsGroup": 65534, "fsGroup": 65534},
                    "containers": [
                        {
                            "name": "prometheus",
                            "image": prom.IMAGE,
                            "args": [
                                "--config.file=/etc/prometheus/prometheus.yml",
                                "--storage.tsdb.path=/prometheus",
                            ],
                            "ports": [{"name": "http", "containerPort": 9090}],
                            "resources": {
                                "requests": {"cpu": "25m", "memory": "128Mi"},
                                "limits": {"cpu": "250m", "memory": "256Mi"},
                            },
                            "readinessProbe": {
                                "httpGet": {"path": "/-/ready", "port": 9090},
                                "periodSeconds": 1,
                            },
                            "volumeMounts": [
                                {
                                    "name": "config",
                                    "mountPath": "/etc/prometheus",
                                    "readOnly": True,
                                },
                                {"name": "data", "mountPath": "/prometheus"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "config", "configMap": {"name": "validation-prometheus"}},
                        {"name": "data", "emptyDir": {}},
                    ],
                },
            },
        },
    }
    rbac = prom.validation_rbac(namespace)["items"]
    for obj in rbac:
        if obj["kind"] == "RoleBinding":
            obj["subjects"][0]["namespace"] = namespace
    mock["items"] += [
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": am.SECRET, "namespace": namespace},
            "type": "Opaque",
            "stringData": {"bot-token": "1:validation-only-token", "chat-id": "1"},
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "telegram-mock-script", "namespace": namespace},
            "data": {"mock_telegram.py": (k8s.ROOT / "scripts/mock_telegram.py").read_text()},
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "telegram-mock", "namespace": namespace},
            "spec": {
                "selector": {"app": "telegram-mock"},
                "ports": [{"port": 9001, "targetPort": 9001}],
            },
        },
        telegram,
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "validation-prometheus", "namespace": namespace},
            "data": {"prometheus.yml": config, "alerts.yml": rules},
        },
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": "prometheus", "namespace": namespace},
        },
        *rbac,
        prometheus,
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "prometheus", "namespace": namespace},
            "spec": {"selector": selector, "ports": [{"port": 9090, "targetPort": 9090}]},
        },
    ]
    return mock


def runtime(namespace):
    pod = fixture.one_pod(namespace, require_ready=True, label=am.LABEL)
    spec = pod["spec"]
    assert spec["automountServiceAccountToken"] is False
    assert [spec["securityContext"][key] for key in ["runAsUser", "runAsGroup", "fsGroup"]] == [
        65534
    ] * 3
    actual = k8s.kube(
        "exec",
        pod["metadata"]["name"],
        "--",
        "sh",
        "-ec",
        'test "$(id -u)" = 65534; test "$(id -g)" = 65534; '
        "printf persistence > /alertmanager/.acceptance-marker; id; uname -m",
        namespace=namespace,
        capture=True,
    )
    assert "aarch64" in actual
    pvc = k8s.kube_json("get", "pvc", "alertmanager-data", namespace=namespace)
    assert pvc["status"]["phase"] == "Bound"
    return {
        "pod": pod["metadata"]["name"],
        "runtime": actual.strip(),
        "pvc_uid": pvc["metadata"]["uid"],
        "resources": spec["containers"][0]["resources"],
    }


def rule_roundtrip(namespace, client, pc):
    def state(name, active=True):
        result = pc.query(f'ALERTS{{alertname="{name}",alertstate="firing"}}')
        return result if active else not result

    fixture.wait_for(
        "isolated healthy forecasts", lambda: fixture.forecast(namespace)["body"]["status"] == "ok"
    )
    fixture.set_mock_mode(namespace, "error", "ok")
    repeated = fixture.wait_for(
        "repeated refresh failure rule", lambda: state("CahuitaRepeatedRefreshFailures")
    )
    wait_message(namespace, "CahuitaRepeatedRefreshFailures")
    # The initial cache is still valid at the repeated-failure notification.
    assert fixture.forecast(namespace)["body"]["weather"]["freshness"] != "unavailable"
    fixture.wait_for(
        "weather source expiration", lambda: state("CahuitaForecastSourceUnusable"), timeout=70
    )
    wait_message(namespace, "CahuitaForecastSourceUnusable")
    assert fixture.ready(namespace)
    assert not state("CahuitaForecastsUnavailable")
    fixture.set_mock_mode(namespace, "ok", "ok")
    fixture.wait_for("weather recovery", lambda: state("CahuitaForecastSourceUnusable", False))
    wait_message(namespace, "CahuitaForecastSourceUnusable", "RESOLVED")
    fixture.wait_for(
        "refresh failure recovery", lambda: state("CahuitaRepeatedRefreshFailures", False)
    )

    fixture.set_mock_mode(namespace, "error", "error")
    fixture.wait_for(
        "both forecasts expired", lambda: state("CahuitaForecastsUnavailable"), timeout=80
    )
    wait_message(namespace, "CahuitaForecastsUnavailable")
    fixture.wait_for("readiness rule firing", lambda: state("CahuitaApplicationNotReady"))
    fixture.wait_for(
        "total-loss inhibition",
        lambda: any(
            a["labels"].get("alertname") == "CahuitaApplicationNotReady"
            and a["status"].get("inhibitedBy")
            for a in client.request("/api/v2/alerts")
        ),
    )
    assert prior.number(pc, "up" + prior.selector(namespace)) == 1
    assert not fixture.ready(namespace)
    fixture.set_mock_mode(namespace, "ok", "ok")
    fixture.wait_for("full forecast recovery", lambda: state("CahuitaForecastsUnavailable", False))
    wait_message(namespace, "CahuitaForecastsUnavailable", "RESOLVED")
    fixture.wait_for(
        "all production-name alert conditions cleared",
        lambda: not pc.query('ALERTS{alertstate="firing"}'),
    )

    k8s.kube("scale", "deployment/cahuita-conditions", "--replicas=0", namespace=namespace)
    try:
        fixture.wait_for(
            "missing application target alert", lambda: state("CahuitaApplicationUnreachable")
        )
        wait_message(namespace, "CahuitaApplicationUnreachable")
    finally:
        k8s.kube("scale", "deployment/cahuita-conditions", "--replicas=1", namespace=namespace)
    k8s.rollout(namespace)
    fixture.wait_for(
        "application target recovery", lambda: state("CahuitaApplicationUnreachable", False)
    )
    wait_message(namespace, "CahuitaApplicationUnreachable", "RESOLVED")
    return {
        "five_rules_observed": True,
        "repeated_failure": repeated,
        "partial_loss_readiness": 200,
        "total_loss_readiness": 503,
        "metrics_during_loss": 1,
        "application_target_absence_and_recovery": "passed",
    }


def test_receiver(namespace, client):
    marker = "NOTIFICATION-TEST-" + uuid.uuid4().hex[:6]
    alert = synthetic("CahuitaNotificationTest", namespace, marker, severity="info")
    alert["labels"]["test"] = "true"
    before = client.notifications("telegram-test")["successful"]
    client.request("/api/v2/alerts", [alert])
    wait_message(namespace, marker)
    assert client.notifications("telegram-test")["successful"] > before
    resolve(client, [alert])
    wait_message(namespace, marker, "RESOLVED")
    assert client.notifications("telegram-test")["successful"] >= before + 2
    return {"receiver": "telegram-test", "completed_deliveries": 2, "private_channel": False}


def delivery_checks(namespace, client):
    marker = "GROUPING-" + uuid.uuid4().hex[:6]
    alerts = [
        synthetic("CahuitaForecastSourceUnusable", namespace, marker, source=s, location="grouping")
        for s in ["weather", "marine"]
    ]
    client.request("/api/v2/alerts", alerts)
    grouped = wait_message(namespace, marker)
    assert (
        len(grouped) == 1
        and "Source: weather" in grouped[0]["text"]
        and "Source: marine" in grouped[0]["text"]
    )
    resolve(client, alerts)
    wait_message(namespace, marker, "RESOLVED")

    fixture.execute(
        namespace,
        """
import json
from pathlib import Path
path=Path('/control/mode.json'); temp=path.with_suffix('.tmp')
temp.write_text(json.dumps({'failures':[500,429]}));temp.replace(path)
print('{}')
""",
        label=MOCK_LABEL,
    )
    retry_marker = "RETRY-" + uuid.uuid4().hex[:6]
    retry = synthetic("CahuitaValidationDelivery", namespace, retry_marker, severity="info")
    client.request("/api/v2/alerts", [retry])
    wait_message(namespace, retry_marker, timeout=90)
    codes = [e["code"] for e in mock_events(namespace) if retry_marker in e["text"]]
    assert 500 in codes and 429 in codes and codes[-1] == 200, codes
    resolve(client, [retry])
    wait_message(namespace, retry_marker, "RESOLVED")
    return {
        "isolated_test_receiver": test_receiver(namespace, client),
        "grouped_sources": 2,
        "grouped_firing_messages": 1,
        "retry_http_codes": codes,
        "recovery_notifications": "passed",
    }


def inhibition_checks(namespace, client):
    results = []
    for name, targets in [
        (
            "CahuitaApplicationUnreachable",
            [
                "CahuitaApplicationNotReady",
                "CahuitaRepeatedRefreshFailures",
                "CahuitaForecastSourceUnusable",
                "CahuitaForecastsUnavailable",
            ],
        ),
        (
            "CahuitaForecastsUnavailable",
            [
                "CahuitaApplicationNotReady",
                "CahuitaRepeatedRefreshFailures",
                "CahuitaForecastSourceUnusable",
            ],
        ),
        ("CahuitaForecastSourceUnusable", ["CahuitaRepeatedRefreshFailures"]),
    ]:
        marker = "INHIBIT-" + uuid.uuid4().hex[:6]
        parent = synthetic(
            name,
            namespace,
            marker,
            source="weather" if name.endswith("SourceUnusable") else None,
            severity="critical" if not name.endswith("SourceUnusable") else "warning",
        )
        children = [
            synthetic(
                target,
                namespace,
                marker,
                source="weather"
                if target in {"CahuitaRepeatedRefreshFailures", "CahuitaForecastSourceUnusable"}
                else None,
                location=None if target == "CahuitaApplicationNotReady" else "cahuita",
            )
            for target in targets
        ]
        client.request("/api/v2/alerts", [parent, *children])

        def suppressed(marker=marker, targets=targets):
            active = [
                a
                for a in client.request("/api/v2/alerts")
                if a["labels"].get("validation_case") == marker
                and a["labels"]["alertname"] in targets
            ]
            return (
                active
                if len(active) == len(targets)
                and all(a["status"].get("inhibitedBy") for a in active)
                else None
            )

        fixture.wait_for("inhibition for " + name, suppressed, timeout=20)
        wait_message(namespace, marker)
        results.append({"source": name, "inhibited": targets})
        resolve(client, [parent, *children])
        fixture.wait_for(
            "inhibition case cleared",
            lambda marker=marker: (
                not any(
                    a["labels"].get("validation_case") == marker
                    and datetime.fromisoformat(a["endsAt"].replace("Z", "+00:00"))
                    > datetime.now(UTC)
                    for a in client.request("/api/v2/alerts")
                )
            ),
            timeout=20,
        )
    return results


def persistence(namespace, client):
    marker = "PERSISTENCE-" + uuid.uuid4().hex[:6]
    alert = synthetic("CahuitaValidationPersistence", namespace, marker, severity="info")
    client.request("/api/v2/alerts", [alert])
    wait_message(namespace, marker)
    now = datetime.now(UTC)
    silence = client.request(
        "/api/v2/silences",
        {
            "matchers": [
                {"name": "namespace", "value": namespace, "isRegex": False, "isEqual": True},
                {
                    "name": "validation_case",
                    "value": "silence-only",
                    "isRegex": False,
                    "isEqual": True,
                },
            ],
            "startsAt": (now - timedelta(seconds=1)).isoformat(),
            "endsAt": (now + timedelta(hours=1)).isoformat(),
            "createdBy": "cahuita-validation",
            "comment": "Temporary isolated persistence check",
        },
    )["silenceID"]
    old = fixture.one_pod(namespace, require_ready=True, label=am.LABEL)
    pvc = k8s.kube_json("get", "pvc", "alertmanager-data", namespace=namespace)["metadata"]["uid"]
    before = len(accepted(namespace, marker, "FIRING"))
    k8s.kube("delete", "pod", old["metadata"]["name"], "--wait=true", namespace=namespace)
    fixture.wait_for(
        "replacement Alertmanager Ready",
        lambda: fixture.one_pod(namespace, require_ready=True, label=am.LABEL),
        timeout=120,
    )
    with am.forward(namespace) as resumed:
        restored = resumed.request("/api/v2/silence/" + silence)
        assert restored["status"]["state"] == "active"
        resumed.request("/api/v2/alerts", [alert])
        # Exceed a full dispatch interval so the persisted notification log must suppress a resend.
        time.sleep(13)
        assert len(accepted(namespace, marker, "FIRING")) == before
        resolve(resumed, [alert])
        wait_message(namespace, marker, "RESOLVED")
        resumed.request("/api/v2/silence/" + silence, method="DELETE")
        assert resumed.request("/api/v2/silence/" + silence)["status"]["state"] == "expired"
        new = fixture.one_pod(namespace, require_ready=True, label=am.LABEL)
        assert new["metadata"]["uid"] != old["metadata"]["uid"]
        assert (
            k8s.kube_json("get", "pvc", "alertmanager-data", namespace=namespace)["metadata"]["uid"]
            == pvc
        )
        assert (
            k8s.kube(
                "exec",
                new["metadata"]["name"],
                "--",
                "cat",
                "/alertmanager/.acceptance-marker",
                namespace=namespace,
                capture=True,
            )
            == "persistence"
        )
    return {
        "pvc_uid": pvc,
        "old_pod": old["metadata"]["name"],
        "new_pod": new["metadata"]["name"],
        "silence_restored_and_removed": True,
        "firing_messages_after_restart": before,
        "notification_log_deduplication": "passed",
    }


def validate():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "real_telegram": "pending_user_configuration",
    }
    namespace = "cahuita-alert-validation-" + uuid.uuid4().hex[:10]
    original = prior.app_identity()
    grafana = k8s.kube_json("get", "deployment", "grafana", namespace="monitoring")["spec"]
    original_pvcs = {
        p["metadata"]["name"]: p["metadata"]["uid"]
        for p in k8s.kube_json("get", "pvc", namespace="monitoring")["items"]
    }
    created = False
    try:
        report["rules"] = alertmanager_rule_tests.run()
        report["amtool"] = am.check().strip()
        k8s.kube("create", "namespace", namespace, "--save-config")
        created = True
        k8s.kube("apply", "-f", "-", input=json.dumps(setup_objects(namespace, original["image"])))
        for deployment in ["mock-provider", "telegram-mock"]:
            k8s.kube(
                "rollout",
                "status",
                "deployment/" + deployment,
                "--timeout=120s",
                namespace=namespace,
            )
        am.deploy(namespace, f"http://telegram-mock.{namespace}.svc.cluster.local:9001")
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
        k8s.kube(
            "rollout", "status", "deployment/prometheus", "--timeout=120s", namespace=namespace
        )
        report["runtime"] = runtime(namespace)
        with am.forward(namespace) as client, prom.forward(namespace) as pc:
            fixture.wait_for("healthy fixture targets", lambda: prior.all_up(pc, [namespace]))
            report["rule_roundtrip"] = rule_roundtrip(namespace, client, pc)
            print(
                "PASS: all five rules, cached-source failure, total loss and application recovery",
                flush=True,
            )
            report["delivery"] = delivery_checks(namespace, client)
            print(
                "PASS: Telegram message grouping, FIRING/RESOLVED and retries after 500/429",
                flush=True,
            )
            report["inhibition"] = inhibition_checks(namespace, client)
            report["queries"] = pc.queries
            report["notifications"] = client.notifications()
            report["persistence"] = persistence(namespace, client)
        report["simulated_messages"] = mock_events(namespace)
        print(
            "PASS: inhibition, PVC/silence persistence and notification deduplication", flush=True
        )
        report["result"] = "passed"
    except Exception as exc:
        report["result"], report["error"] = "failed", str(exc)
        if created:
            am.diagnostics(namespace)
            k8s.kube("logs", "deployment/prometheus", "--tail=40", namespace=namespace, check=False)
        raise
    finally:
        try:
            if created:
                k8s.kube("delete", "namespace", namespace, "--timeout=90s")
            assert prior.app_identity() == original, (
                "Real application changed during isolated validation"
            )
            assert (
                k8s.kube_json("get", "deployment", "grafana", namespace="monitoring")["spec"]
                == grafana
            )
            for name, uid in original_pvcs.items():
                assert (
                    k8s.kube_json("get", "pvc", name, namespace="monitoring")["metadata"]["uid"]
                    == uid
                )
            report["real_application_unchanged"] = original
            report["existing_volumes_and_grafana_preserved"] = True
        except Exception as exc:
            report["result"], report["cleanup_error"] = "failed", str(exc)
            raise
        finally:
            report["finished_at"] = datetime.now(UTC).isoformat()
            (OUTPUT / "validation.json").write_text(json.dumps(report, indent=2) + "\n")
            print(f"Evidence saved to {OUTPUT / 'validation.json'}", flush=True)
