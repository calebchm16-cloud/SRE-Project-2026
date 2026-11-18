"""Portable Kubernetes fixtures for CI, notification tests and isolated demos."""

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager

import k8s

REPORT = k8s.ROOT / "artifacts/k8s-validation.json"

def wait_for(description, function, timeout=120):
    deadline, last_error = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            result = function()
            if result:
                return result
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
            last_error = exc
        time.sleep(0.5)
    raise TimeoutError(f"Timed out waiting for {description}; last error: {last_error}")

def pods(namespace, label=k8s.LABEL):
    return k8s.kube_json("get", "pods", "-l", label, namespace=namespace)["items"]

def one_pod(namespace, *, require_ready=False, label=k8s.LABEL):
    active = [pod for pod in pods(namespace, label) if not pod["metadata"].get("deletionTimestamp")]
    if len(active) != 1 or active[0]["status"]["phase"] != "Running":
        raise RuntimeError("Expected exactly one running application pod")
    pod = active[0]
    if require_ready and not any(
        condition["type"] == "Ready" and condition["status"] == "True"
        for condition in pod["status"].get("conditions", [])
    ):
        raise RuntimeError("Pod is not Ready")
    return pod

def execute(namespace, code, *args, label=k8s.LABEL):
    pod = one_pod(namespace, label=label)["metadata"]["name"]
    return json.loads(
        k8s.kube(
            "exec",
            pod,
            "--",
            "python",
            "-c",
            code,
            *args,
            namespace=namespace,
            capture=True,
            timeout=25,
        )
    )

def pod_http(namespace, path):
    return execute(
        namespace,
        """
import json,sys,urllib.request,urllib.error
try:
    response=urllib.request.urlopen('http://127.0.0.1:8000'+sys.argv[1],timeout=3)
except urllib.error.HTTPError as error:
    response=error
with response:
    body=response.read().decode()
    content_type=response.headers.get('Content-Type','')
    parsed=json.loads(body) if 'application/json' in content_type else body
    print(json.dumps({'status':response.status,'type':content_type,'body':parsed}))
""",
        path,
    )

def forecast(namespace):
    return pod_http(namespace, "/api/v1/locations/cahuita/forecast")

def ready(namespace):
    return pod_http(namespace, "/health/ready")["status"] == 200

def storage_probe(namespace, marker=None):
    return execute(
        namespace,
        """
import json,os,platform,sqlite3,sys
from pathlib import Path
path=Path('/data/.k8s-validation-marker')
if sys.argv[1] != 'read':path.write_text(sys.argv[1])
with sqlite3.connect('/data/cache.sqlite3') as db:
    check=db.execute('PRAGMA quick_check').fetchone()[0]
    rows=db.execute('SELECT COUNT(*) FROM cache').fetchone()[0]
print(json.dumps({'uid':os.getuid(),'gid':os.getgid(),'architecture':platform.machine(),
                  'marker':path.read_text(),'sqlite_check':check,'cache_rows':rows}))
""",
        marker or "read",
    )

@contextmanager
def port_forward():
    with (k8s.ROOT / "artifacts/k8s-port-forward.log").open("w") as log:
        process = subprocess.Popen(
            [
                str(k8s.kubectl_path()),
                "--context",
                k8s.CONTEXT,
                "--namespace",
                k8s.NAMESPACE,
                "port-forward",
                "--address=127.0.0.1",
                "service/cahuita-conditions",
                "8080:8000",
            ],
            stdout=log,
            stderr=log,
        )
        try:

            def started():
                if process.poll() is not None:
                    raise RuntimeError(
                        "Port forward exited; inspect artifacts/k8s-port-forward.log"
                    )
                with urllib.request.urlopen(
                    "http://127.0.0.1:8080/health/live", timeout=2
                ) as reply:
                    return reply.status == 200

            wait_for("localhost port-forward", started, timeout=20)
            yield
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

def validate_local_access():
    results = {}
    with port_forward():
        for path in [
            "/",
            "/api/v1/locations",
            "/api/v1/locations/cahuita/forecast",
            "/api/v1/status",
            "/health/live",
            "/health/ready",
            "/metrics",
            "/docs",
            "/openapi.json",
            "/static/vendor/bootstrap.min.css",
        ]:
            with urllib.request.urlopen("http://127.0.0.1:8080" + path, timeout=5) as reply:
                body = reply.read().decode()
                assert reply.status == 200, path
                results[path] = {
                    "status": reply.status,
                    "content_type": reply.headers.get("Content-Type"),
                }
                if path == "/":
                    assert 'method="get"' in body and "table-condensed table-bordered" in body
                if path == "/metrics":
                    assert (
                        "process_resident_memory_bytes" in body and "forecast_data_usable" in body
                    )
    return results

def validate_recreate(namespace):
    initial = one_pod(namespace, require_ready=True)["metadata"]["uid"]
    observations, errors, stopped = [], [], threading.Event()

    def observe():
        while not stopped.is_set():
            try:
                current = pods(namespace)
                running = [
                    pod["metadata"]["uid"]
                    for pod in current
                    if any(
                        "running" in container.get("state", {})
                        for container in pod["status"].get("containerStatuses", [])
                    )
                ]
                observations.append(running)
            except Exception as exc:
                errors.append(str(exc))
            stopped.wait(0.15)

    thread = threading.Thread(target=observe, daemon=True)
    thread.start()
    try:
        k8s.kube("rollout", "restart", "deployment/cahuita-conditions", namespace=namespace)
        k8s.rollout(namespace)
    finally:
        stopped.set()
        thread.join(timeout=30)
    final = one_pod(namespace, require_ready=True)["metadata"]["uid"]
    assert initial != final
    assert observations and not errors, errors
    assert max(map(len, observations)) <= 1, observations
    return {
        "old_pod_uid": initial,
        "new_pod_uid": final,
        "samples": len(observations),
        "maximum_running_pods": max(map(len, observations)),
    }

def mock_objects(namespace, image):
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "mock-provider-script", "namespace": namespace},
                "data": {"mock_provider.py": (k8s.ROOT / "scripts/mock_provider.py").read_text()},
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "mock-provider", "namespace": namespace},
                "spec": {
                    "selector": {"app": "mock-provider"},
                    "ports": [{"port": 9000, "targetPort": 9000}],
                },
            },
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "mock-provider", "namespace": namespace},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": "mock-provider"}},
                    "template": {
                        "metadata": {"labels": {"app": "mock-provider"}},
                        "spec": {
                            "automountServiceAccountToken": False,
                            "terminationGracePeriodSeconds": 2,
                            "securityContext": {
                                "runAsUser": 10001,
                                "runAsGroup": 10001,
                                "fsGroup": 10001,
                            },
                            "containers": [
                                {
                                    "name": "mock",
                                    "image": image,
                                    "imagePullPolicy": "Never",
                                    "command": [
                                        "python",
                                        "/fixture/mock_provider.py",
                                        "--host",
                                        "0.0.0.0",
                                        "--mode-file",
                                        "/control/mode.json",
                                    ],
                                    "ports": [{"containerPort": 9000}],
                                    "resources": {
                                        "requests": {"cpu": "25m", "memory": "32Mi"},
                                        "limits": {"cpu": "250m", "memory": "96Mi"},
                                    },
                                    "readinessProbe": {
                                        "tcpSocket": {"port": 9000},
                                        "periodSeconds": 1,
                                    },
                                    "volumeMounts": [
                                        {
                                            "name": "script",
                                            "mountPath": "/fixture",
                                            "readOnly": True,
                                        },
                                        {"name": "control", "mountPath": "/control"},
                                    ],
                                }
                            ],
                            "volumes": [
                                {"name": "script", "configMap": {"name": "mock-provider-script"}},
                                {"name": "control", "emptyDir": {}},
                            ],
                        },
                    },
                },
            },
        ],
    }

def set_mock_mode(namespace, weather, marine):
    execute(
        namespace,
        """
import json,sys
from pathlib import Path
path=Path('/control/mode.json')
temporary=path.with_suffix('.tmp')
temporary.write_text(json.dumps({'weather':sys.argv[1],'marine':sys.argv[2]}))
temporary.replace(path)
print('{}')
""",
        weather,
        marine,
        label="app=mock-provider",
    )

def validate_failure_recovery(image):
    namespace = "cahuita-validation-" + uuid.uuid4().hex[:10]
    created = False
    try:
        k8s.kube("create", "namespace", namespace)
        created = True
        k8s.kube("apply", "-f", "-", input=json.dumps(mock_objects(namespace, image)))
        k8s.kube(
            "rollout", "status", "deployment/mock-provider", "--timeout=120s", namespace=namespace
        )
        k8s.apply(
            image,
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
        wait_for("mock forecasts", lambda: forecast(namespace)["body"]["status"] == "ok")
        set_mock_mode(namespace, "error", "error")
        wait_for(
            "both provider failures",
            lambda: all(
                forecast(namespace)["body"][source]["last_outcome"] == "http_error"
                for source in ("weather", "marine")
            ),
        )
        original = forecast(namespace)["body"]
        old_pod = one_pod(namespace)["metadata"]["name"]
        k8s.kube("scale", "deployment/cahuita-conditions", "--replicas=0", namespace=namespace)
        k8s.kube("wait", "--for=delete", f"pod/{old_pod}", "--timeout=60s", namespace=namespace)
        k8s.kube("scale", "deployment/cahuita-conditions", "--replicas=1", namespace=namespace)
        k8s.rollout(namespace)
        restored = forecast(namespace)
        assert restored["status"] == 200 and ready(namespace), restored
        for source in ("weather", "marine"):
            assert (
                restored["body"][source]["last_success_at"] == original[source]["last_success_at"]
            )

        def expired_forecast():
            response = forecast(namespace)
            return response if response["status"] == 503 else None

        expired = wait_for("cache expiration", expired_forecast, timeout=70)
        wait_for("readiness failure", lambda: not ready(namespace), timeout=15)

        def kubernetes_unready():
            pod = one_pod(namespace)
            return (
                pod
                if not any(
                    condition["type"] == "Ready" and condition["status"] == "True"
                    for condition in pod["status"].get("conditions", [])
                )
                else None
            )

        unready_pod = wait_for("Kubernetes readiness removal", kubernetes_unready, timeout=20)
        assert pod_http(namespace, "/health/live")["status"] == 200
        assert pod_http(namespace, "/")["status"] == 200
        assert all(
            status["restartCount"] == 0 for status in unready_pod["status"]["containerStatuses"]
        )
        assert "forecast_data_usable" in pod_http(namespace, "/metrics")["body"]
        set_mock_mode(namespace, "ok", "error")
        wait_for(
            "partial recovery",
            lambda: (
                (response := forecast(namespace))["status"] == 200
                and response["body"]["weather"]["freshness"] == "fresh"
                and response["body"]["marine"]["freshness"] == "unavailable"
            ),
        )
        wait_for("partial readiness", lambda: ready(namespace))
        set_mock_mode(namespace, "ok", "ok")
        wait_for("complete recovery", lambda: forecast(namespace)["body"]["status"] == "ok")
        k8s.rollout(namespace)
        return {
            "namespace": namespace,
            "cached_restart": "passed",
            "expired_forecast_http_status": expired["status"],
            "liveness_while_unready": 200,
            "partial_recovery": "passed",
            "full_recovery": "passed",
        }
    except Exception:
        if created:
            k8s.diagnostics(namespace)
        raise
    finally:
        if created and namespace.startswith("cahuita-validation-"):
            k8s.kube("delete", "namespace", namespace, "--timeout=90s")

def ensure_live_sources_after_resume():
    action = "already_fresh"
    if forecast(k8s.NAMESPACE)["body"]["status"] != "ok":
        # A resumed app may start before DNS/egress is usable. Cache retention is checked
        # separately before this explicit refresh, rather than hidden by the new fetch.
        k8s.kube(
            "wait",
            "--for=condition=Ready",
            "pods",
            "-l",
            "k8s-app=kube-dns",
            "--timeout=120s",
            namespace="kube-system",
        )
        k8s.kube("rollout", "restart", "deployment/cahuita-conditions", namespace=k8s.NAMESPACE)
        k8s.rollout()
        wait_for(
            "fresh real forecasts after resume",
            lambda: forecast(k8s.NAMESPACE)["body"]["status"] == "ok",
            timeout=60,
        )
        action = "restarted_after_network_startup_failure"
    current = forecast(k8s.NAMESPACE)["body"]
    return {
        "action": action,
        "status": current["status"],
        "sources": {source: current[source]["last_outcome"] for source in ("weather", "marine")},
    }
