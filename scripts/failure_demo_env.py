"""Isolated Kubernetes fixtures and localhost access for the failure demonstration."""

import base64
import copy
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import alertmanager as am
import alertmanager_validate as alerts
import grafana
import k8s_validate as fixture
import prometheus as prom

import k8s

BASE = k8s.ROOT / "artifacts/failure-demo"
OWNER = "cahuita.conditions/demo-run"
PHASES = ("healthy", "cached", "partial", "unavailable", "recovered")
PORTS = {"app": 18080, "prometheus": 19090, "alertmanager": 19093, "grafana": 13000}
CAPTURES = {
    "healthy": ["01-healthy-app.png"],
    "cached": ["02-cached-app.png", "08-prometheus-firing.png"],
    "partial": ["03-partial-app.png"],
    "unavailable": [
        "04-unavailable-app.png",
        "09-alertmanager-firing.png",
        "11-simulated-firing.png",
    ],
    "recovered": [
        "05-recovered-app.png",
        "06-traffic-history.png",
        "07-forecast-history.png",
        "10-alertmanager-resolved.png",
        "12-simulated-resolved.png",
    ],
}


def directory(run_id):
    if not re.fullmatch(r"[a-f0-9]{10}", run_id):
        raise ValueError("Expected the ten-character run ID printed by start")
    return BASE / run_id


def load(run_id):
    path = directory(run_id)
    state = json.loads((path / "state.json").read_text())
    if state["run_id"] != run_id or state["namespace"] != "cahuita-alert-validation-" + run_id:
        raise ValueError("Run state does not match its owned namespace")
    return state


def save(state):
    path = directory(state["run_id"])
    temp = path / "state.tmp"
    temp.write_text(json.dumps(state, indent=2) + "\n")
    temp.replace(path / "state.json")


def ownership(state):
    ns = k8s.kube_json("get", "namespace", state["namespace"])
    if (
        ns["metadata"]["uid"] != state["namespace_uid"]
        or ns["metadata"].get("labels", {}).get(OWNER) != state["run_id"]
    ):
        raise RuntimeError("Namespace ownership changed; refusing to mutate or delete it")
    return ns


def preserved_workloads():
    result = {}
    for namespace in (k8s.NAMESPACE, "monitoring"):
        deployments = k8s.kube_json("get", "deployments", namespace=namespace)["items"]
        result[namespace] = {
            "deployments": {obj["metadata"]["name"]: obj["spec"] for obj in deployments},
            "pods": sorted(
                obj["metadata"]["uid"]
                for obj in k8s.kube_json("get", "pods", namespace=namespace)["items"]
            ),
            "pvcs": {
                obj["metadata"]["name"]: obj["metadata"]["uid"]
                for obj in k8s.kube_json("get", "pvc", namespace=namespace)["items"]
            },
        }
    return result


def grafana_manifest(state):
    """Reprovision official dashboard copies in the demo namespace, never in monitoring."""
    path = directory(state["run_id"])
    source = grafana.BASE
    with tempfile.TemporaryDirectory(prefix="grafana-demo-", dir=path) as temporary:
        temp = Path(temporary)
        (temp / "datasource.yml").write_text(
            (source / "datasource.yml")
            .read_text()
            .replace(
                "prometheus.monitoring.svc.cluster.local",
                f"prometheus.{state['namespace']}.svc.cluster.local",
            )
        )
        (temp / "dashboards.yml").write_text(
            (source / "dashboards.yml")
            .read_text()
            .replace("folder: Cahuita Conditions", "folder: Cahuita Conditions - Failure Demo")
        )
        for doc in grafana.dashboards():
            doc = copy.deepcopy(doc)
            doc["title"] += " [DEMO]"
            doc["tags"].append("failure-demo")
            doc["time"] = {"from": "now-15m", "to": "now"}
            for variable in doc["templating"]["list"]:
                if variable["name"] == "namespace":
                    variable["current"] = {"text": state["namespace"], "value": state["namespace"]}
            for panel in doc["panels"]:
                panel["description"] = (
                    panel.get("description", "")
                    .replace("30 minutes", "15 seconds (demo)")
                    .replace("six hours", "120 seconds (demo)")
                )
                for step in panel["fieldConfig"]["defaults"].get("thresholds", {}).get("steps", []):
                    if step["value"] == 1800:
                        step["value"] = 15
                    elif step["value"] == 21600:
                        step["value"] = 120
            filename = (
                "application.json" if doc["uid"] == "cahuita-application" else "forecasts.json"
            )
            (temp / filename).write_text(json.dumps(doc, indent=2) + "\n")
            (path / filename).write_text(json.dumps(doc, indent=2) + "\n")
        overlay = {
            "apiVersion": "kustomize.config.k8s.io/v1beta1",
            "kind": "Kustomization",
            "namespace": state["namespace"],
            "resources": [os.path.relpath(source, temp)],
            "configMapGenerator": [
                {
                    "name": "grafana-provisioning",
                    "namespace": "monitoring",
                    "behavior": "replace",
                    "files": ["datasource.yml", "dashboards.yml"],
                },
                {
                    "name": "grafana-dashboards",
                    "namespace": "monitoring",
                    "behavior": "replace",
                    "files": ["application.json", "forecasts.json"],
                },
            ],
        }
        (temp / "kustomization.yaml").write_text(json.dumps(overlay))
        return k8s.run([k8s.kubectl_path(), "kustomize", temp], capture=True)


def prepare(state):
    path, namespace = directory(state["run_id"]), state["namespace"]
    ns = k8s.kube(
        "create",
        "-f",
        "-",
        "-o",
        "json",
        capture=True,
        input=json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": namespace, "labels": {OWNER: state["run_id"]}},
            }
        ),
    )
    state["namespace_uid"] = json.loads(ns)["metadata"]["uid"]
    save(state)
    objects = alerts.setup_objects(namespace, state["image"], output=path)
    prometheus = next(
        obj
        for obj in objects["items"]
        if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "prometheus"
    )
    objects["items"].remove(prometheus)
    # Start scraping with an empty rule bundle. Kubernetes discovery is asynchronous;
    # even a warmed application can initially be absent from Prometheus's target set.
    pod_spec = prometheus["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    container["command"] = [
        "/bin/sh",
        "-ec",
        "cp /bootstrap/prometheus.yml /etc/prometheus/prometheus.yml; "
        'printf "groups: []\\n" > /etc/prometheus/alerts.yml; '
        'exec /bin/prometheus "$@"',
        "prometheus-demo",
    ]
    container["volumeMounts"][0]["mountPath"] = "/bootstrap"
    container["volumeMounts"].append({"name": "runtime-config", "mountPath": "/etc/prometheus"})
    pod_spec["volumes"].append({"name": "runtime-config", "emptyDir": {}})
    # Existing accelerated fixtures; descriptions must describe these same demo windows.
    configmap = next(
        obj for obj in objects["items"] if obj["metadata"]["name"] == "validation-prometheus"
    )
    configmap["data"]["alerts.yml"] = (
        configmap["data"]["alerts.yml"]
        .replace("in one hour", "in one minute (demo)")
        .replace("last 30 minutes", "last 10 seconds (demo)")
        .replace("one-minute window", "ten-second demo window")
    )
    for key, text in configmap["data"].items():
        (path / key).write_text(text)
    checker = [
        "docker",
        "--context",
        k8s.CONTEXT,
        "run",
        "--rm",
        "--network=none",
        "--entrypoint=/bin/promtool",
        "--mount",
        f"type=bind,source={path},target=/etc/prometheus,readonly",
        prom.IMAGE,
    ]
    checked = k8s.run(
        checker + ["check", "config", "/etc/prometheus/prometheus.yml"], capture=True
    ) + k8s.run(checker + ["check", "rules", "/etc/prometheus/alerts.yml"], capture=True)
    (path / "promtool-check.txt").write_text(checked)
    # This creates no apply annotation containing a credential. The password is demo-only.
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "grafana-admin", "namespace": namespace},
        "stringData": {"admin-password": secrets.token_urlsafe(32)},
    }
    proc = subprocess.run(
        [str(k8s.kubectl_path()), "--context", k8s.CONTEXT, "create", "-f", "-"],
        input=json.dumps(secret),
        text=True,
        capture_output=True,
    )
    if proc.returncode:
        raise RuntimeError("Could not create the isolated Grafana credential; content omitted")
    k8s.kube("apply", "-f", "-", input=json.dumps(objects))
    for name in ("mock-provider", "telegram-mock"):
        k8s.kube("rollout", "status", "deployment/" + name, "--timeout=120s", namespace=namespace)
    am.deploy(namespace, f"http://telegram-mock.{namespace}.svc.cluster.local:9001")
    manifest = grafana_manifest(state)
    (path / "grafana-manifest.yml").write_text(manifest)
    k8s.kube("apply", "-f", "-", input=manifest)
    k8s.apply(
        state["image"],
        namespace,
        settings={
            "CAHUITA_WEATHER_BASE_URL": "http://mock-provider:9000",
            "CAHUITA_MARINE_BASE_URL": "http://mock-provider:9000",
            "CAHUITA_REFRESH_SECONDS": "1",
            "CAHUITA_STALE_SECONDS": "15",
            "CAHUITA_EXPIRE_SECONDS": "120",
            "CAHUITA_REQUEST_TIMEOUT_SECONDS": "1",
            "CAHUITA_RETRY_DELAY_SECONDS": "0",
        },
    )
    k8s.rollout(namespace)
    fixture.wait_for(
        "warm weather and marine data",
        lambda: fixture.forecast(namespace)["body"]["status"] == "ok",
    )
    # Start rule evaluation only after the application is healthy.
    k8s.kube("apply", "-f", "-", input=json.dumps(prometheus))
    for name in ("prometheus", "grafana"):
        k8s.kube("rollout", "status", "deployment/" + name, "--timeout=240s", namespace=namespace)
    ownership(state)


def activate_rules(state):
    ownership(state)
    # The same unprivileged process owns this emptyDir. Reload through SIGHUP,
    # leaving administrative and HTTP reload APIs disabled.
    k8s.kube(
        "exec",
        "deployment/prometheus",
        "--",
        "/bin/sh",
        "-ec",
        "cp /bootstrap/alerts.yml /etc/prometheus/alerts.yml; kill -HUP 1",
        namespace=state["namespace"],
        capture=True,
    )


def grafana_password(state):
    ownership(state)
    secret = k8s.kube_json("get", "secret", "grafana-admin", namespace=state["namespace"])
    return base64.b64decode(secret["data"]["admin-password"]).decode()


class ForwardURLs(dict):
    def check(self):
        if any(process.poll() is not None for process in self.processes):
            raise RuntimeError("A demo port-forward ended; access is closing")


@contextmanager
def forwards(state, fixed=False):
    ownership(state)
    pod = fixture.one_pod(state["namespace"])["metadata"]["name"]
    processes, handles, urls = [], [], ForwardURLs()
    urls.processes = processes
    try:
        for name, target, remote in [
            ("app", "pod/" + pod, 8000),
            ("prometheus", "service/prometheus", 9090),
            ("alertmanager", "service/alertmanager", 9093),
            ("grafana", "service/grafana", 3000),
        ]:
            log = tempfile.TemporaryFile(mode="w+")
            handles.append(log)
            proc = subprocess.Popen(
                [
                    str(k8s.kubectl_path()),
                    "--context",
                    k8s.CONTEXT,
                    "--namespace",
                    state["namespace"],
                    "port-forward",
                    "--address=127.0.0.1",
                    target,
                    f"{PORTS[name] if fixed else ''}:{remote}",
                ],
                stdout=log,
                stderr=log,
            )
            processes.append(proc)
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                log.seek(0)
                output = log.read()
                if proc.poll() is not None:
                    raise RuntimeError(f"{name} port-forward exited: {output}")
                match = re.search(r"Forwarding from 127\.0\.0\.1:(\d+)", output)
                if match:
                    urls[name] = "http://127.0.0.1:" + match[1]
                    break
                time.sleep(0.1)
            else:
                raise TimeoutError(f"{name} port-forward did not start")
        yield urls
    finally:
        for proc in processes:
            proc.terminate()
        for proc in processes:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for log in handles:
            log.close()


def http(url):
    try:
        response = urllib.request.urlopen(url, timeout=10)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        text = response.read().decode()
        body = (
            json.loads(text)
            if "application/json" in response.headers.get("Content-Type", "")
            else text
        )
        return {"status": response.status, "body": body}
