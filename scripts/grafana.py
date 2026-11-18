#!/usr/bin/env python3
"""Operate file-provisioned Grafana in the explicit Cahuita Kubernetes context."""

import argparse
import base64
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import k8s

NAMESPACE = "monitoring"
LABEL = "app.kubernetes.io/name=grafana"
BASE = k8s.ROOT / "monitoring/grafana"
DATASOURCE = "cahuita-prometheus"
DASHBOARDS = ("cahuita-application", "cahuita-forecasts")


def dashboards():
    return [json.loads(path.read_text()) for path in sorted((BASE / "dashboards").glob("*.json"))]


def render(startup_failure_threshold=None):
    if startup_failure_threshold is None:
        return k8s.run([k8s.kubectl_path(), "kustomize", BASE], capture=True)
    if not isinstance(startup_failure_threshold, int) or not 1 <= startup_failure_threshold <= 300:
        raise ValueError("Invalid startup probe failure threshold")
    with tempfile.TemporaryDirectory(prefix="grafana-startup-", dir=k8s.ROOT / "artifacts") as temp:
        path = Path(temp)
        overlay = {
            "apiVersion": "kustomize.config.k8s.io/v1beta1",
            "kind": "Kustomization",
            "resources": [os.path.relpath(BASE, path)],
            "patches": [
                {
                    "target": {"kind": "Deployment", "name": "grafana"},
                    "patch": json.dumps(
                        [
                            {
                                "op": "replace",
                            "path": (
                                "/spec/template/spec/containers/0/startupProbe/"
                                "failureThreshold"
                            ),
                                "value": startup_failure_threshold,
                            }
                        ]
                    ),
                }
            ],
        }
        (path / "kustomization.yaml").write_text(json.dumps(overlay))
        return k8s.run([k8s.kubectl_path(), "kustomize", path], capture=True)


def check():
    documents = dashboards()
    if {doc["uid"] for doc in documents} != set(DASHBOARDS) or len(documents) != 2:
        raise ValueError("Expected exactly the two stable Cahuita dashboard UIDs")
    count = 0
    for doc in documents:
        if doc["editable"] or doc["timezone"] != "America/Costa_Rica":
            raise ValueError("Dashboards must be read-only and use the Cahuita timezone")
        ids = [panel["id"] for panel in doc["panels"]]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate panel ID")
        variables = {item["name"] for item in doc["templating"]["list"]}
        for panel in doc["panels"]:
            if panel["type"] not in {"stat", "timeseries", "table", "bargauge"}:
                raise ValueError("Only bundled panels are supported")
            if panel["fieldConfig"]["defaults"]["noValue"] != "No data":
                raise ValueError("Missing values must remain explicit")
            for target in panel["targets"]:
                expr = target["expr"]
                if target["datasource"]["uid"] != DATASOURCE or 'job="cahuita"' not in expr:
                    raise ValueError("Queries must use the Cahuita datasource/job")
                unknown = set(re.findall(r"\$\{([a-z_]+)\}", expr)) - variables
                if unknown:
                    raise ValueError(f"Undefined dashboard variables: {unknown}")
                count += 1
    render()
    return {
        "dashboards": len(documents),
        "panels": sum(len(d["panels"]) for d in documents),
        "queries": count,
    }


def ensure_credentials():
    """Bootstrap once. Never emit secret data, even on a failed create."""
    existing = k8s.kube(
        "get",
        "secret",
        "grafana-admin",
        "--ignore-not-found",
        "-o",
        "name",
        namespace=NAMESPACE,
        capture=True,
    )
    if existing.strip():
        return
    obj = {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {"name": "grafana-admin", "namespace": NAMESPACE},
        "stringData": {"admin-password": secrets.token_urlsafe(32)},
    }
    # Do not use kubectl apply: its last-applied annotation would duplicate the credential.
    command = [str(k8s.kubectl_path()), "--context", k8s.CONTEXT, "create", "-f", "-"]
    result = subprocess.run(command, input=json.dumps(obj), text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(
            "Could not create the Grafana bootstrap Secret; no credential was logged"
        )
    print("Created Grafana bootstrap Secret (credential omitted)", flush=True)


def password():
    # Called internally for authenticated checks; never included in evidence or diagnostics.
    secret = k8s.kube_json("get", "secret", "grafana-admin", namespace=NAMESPACE)
    return base64.b64decode(secret["data"]["admin-password"]).decode()


def diagnostics():
    for args in [
        ["get", "deployment,pods,pvc,svc", "-l", LABEL, "-o", "wide"],
        ["describe", "pods", "-l", LABEL],
        ["logs", "deployment/grafana", "--tail=80"],
    ]:
        k8s.kube(*args, namespace=NAMESPACE, check=False)


def rollout(timeout="240s"):
    try:
        k8s.kube(
            "rollout", "status", "deployment/grafana", f"--timeout={timeout}", namespace=NAMESPACE
        )
    except subprocess.CalledProcessError:
        diagnostics()
        raise


def deploy(rollout_timeout="240s", startup_failure_threshold=None):
    print(json.dumps(check()), flush=True)
    # Monitoring/Prometheus must already exist; don't overwrite its namespace metadata.
    k8s.kube("get", "service", "prometheus", namespace=NAMESPACE, capture=True)
    ensure_credentials()
    manifest = render(startup_failure_threshold)
    k8s.kube("apply", "--dry-run=server", "-f", "-", input=manifest)
    k8s.kube("apply", "-f", "-", input=manifest)
    rollout(rollout_timeout)


@contextmanager
def forward(port=None):
    """Use an ephemeral localhost port for API checks, or an explicit port for UI access."""
    with tempfile.TemporaryFile(mode="w+") as log:
        process = subprocess.Popen(
            [
                str(k8s.kubectl_path()),
                "--context",
                k8s.CONTEXT,
                "--namespace",
                NAMESPACE,
                "port-forward",
                "--address=127.0.0.1",
                "service/grafana",
                f"{port or ''}:3000",
            ],
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                log.seek(0)
                output = log.read()
                if process.poll() is not None:
                    raise RuntimeError(f"Grafana port-forward exited: {output}")
                match = re.search(r"Forwarding from 127\.0\.0\.1:(\d+)", output)
                if match:
                    yield Client(f"http://127.0.0.1:{match[1]}", password())
                    return
                time.sleep(0.1)
            raise TimeoutError("Grafana port-forward did not start")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


class Client:
    def __init__(self, url, admin_password):
        self.url = url
        self._authorization = (
            "Basic " + base64.b64encode(f"admin:{admin_password}".encode()).decode()
        )
        self.queries = []

    def request(self, path, data=None):
        headers = {"Authorization": self._authorization}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.url + path,
            headers=headers,
            data=None if data is None else json.dumps(data).encode(),
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    def query(self, expr, at=None):
        params = {"query": expr}
        if at is not None:
            params["time"] = str(at)
        result = self.request(
            f"/api/datasources/proxy/uid/{DATASOURCE}/api/v1/query?"
            + urllib.parse.urlencode(params)
        )
        if result.get("status") != "success":
            raise RuntimeError(f"Grafana datasource query failed: {result.get('error')}")
        self.queries.append({"query": expr, "time": at, "data": result["data"]})
        return result["data"]["result"]


def interpolate(
    expr,
    *,
    namespace="cahuita-conditions",
    pod=".*",
    route=".*",
    location="cahuita",
    source=".*",
    rate_interval="1m",
    duration="6h",
):
    # API checks supply regex values explicitly; Grafana's browser does its own interpolation.
    for key, value in {
        "namespace": namespace,
        "pod": pod,
        "route": route,
        "location": location,
        "source": source,
    }.items():
        expr = expr.replace("${" + key + "}", value)
    return expr.replace("$__rate_interval", rate_interval).replace("$__range", duration)


def remove_workload():
    k8s.kube(
        "delete",
        "deployment,service,configmap",
        "-l",
        LABEL,
        "--ignore-not-found",
        namespace=NAMESPACE,
    )
    print("Grafana workload removed. grafana-data PVC and grafana-admin Secret retained.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "check",
            "render",
            "deploy",
            "status",
            "access",
            "credentials",
            "validate",
            "remove-workload",
        ],
    )
    args = parser.parse_args()
    if args.command == "check":
        print(json.dumps(check(), indent=2))
    elif args.command == "render":
        print(render(), end="")
    elif args.command == "deploy":
        deploy()
        print("Grafana deployed. Run: python3 scripts/grafana.py access")
    elif args.command == "status":
        k8s.kube("get", "deployment,pods,pvc,svc", "-l", LABEL, namespace=NAMESPACE)
        with forward() as client:
            print(
                json.dumps(
                    {
                        "health": client.request("/api/health"),
                        "datasource": client.request(f"/api/datasources/uid/{DATASOURCE}/health"),
                        "dashboards": client.request("/api/search?tag=cahuita"),
                    },
                    indent=2,
                )
            )
    elif args.command == "access":
        k8s.kube(
            "port-forward",
            "--address=127.0.0.1",
            "service/grafana",
            "3000:3000",
            namespace=NAMESPACE,
        )
    elif args.command == "credentials":
        print(f"Username: admin\nInitial password: {password()}")
    elif args.command == "validate":
        from grafana_validate import validate

        validate()
    elif args.command == "remove-workload":
        remove_workload()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        sys.exit(f"Grafana operation failed: {exc}")
