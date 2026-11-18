#!/usr/bin/env python3
"""Operate standalone Prometheus in the explicit Cahuita Kubernetes context."""

import argparse
import json
import os
import re
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
LABEL = "app.kubernetes.io/name=prometheus"
IMAGE = "prom/prometheus:v3.13.5"
BASE = k8s.ROOT / "monitoring/prometheus"


def configuration(extra_namespace=None):
    config = (BASE / "prometheus.yml").read_text()
    if extra_namespace:
        if not re.fullmatch(r"cahuita-prom-validation-[a-f0-9]{10}", extra_namespace):
            raise ValueError("Additional discovery is restricted to an owned validation namespace")
        original = "names: [cahuita-conditions]"
        if config.count(original) != 1:
            raise ValueError("Expected exactly one namespace discovery list")
        config = config.replace(original, f"names: [cahuita-conditions, {extra_namespace}]")
    return config


def validation_rbac(namespace):
    """Same pod-only permissions as production, restricted to the temporary namespace."""
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": {"name": "prometheus-pod-reader", "namespace": namespace},
                "rules": [
                    {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]}
                ],
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": {"name": "prometheus-pod-reader", "namespace": namespace},
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "Role",
                    "name": "prometheus-pod-reader",
                },
                "subjects": [
                    {"kind": "ServiceAccount", "name": "prometheus", "namespace": NAMESPACE}
                ],
            },
        ],
    }


def render(extra_namespace=None):
    config = configuration(extra_namespace)
    (k8s.ROOT / "artifacts").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="prometheus-kustomize-", dir=k8s.ROOT / "artifacts"
    ) as temporary:
        path = Path(temporary)
        overlay = {
            "apiVersion": "kustomize.config.k8s.io/v1beta1",
            "kind": "Kustomization",
            "resources": [os.path.relpath(BASE, path)],
        }
        if extra_namespace:
            (path / "prometheus.yml").write_text(config)
            # Discovery-only validation must not enable alert rules for its fixture namespace.
            (path / "alerts.yml").write_text((BASE / "alerts.yml").read_text())
            (path / "validation-rbac.json").write_text(json.dumps(validation_rbac(extra_namespace)))
            overlay["resources"].append("validation-rbac.json")
            overlay["configMapGenerator"] = [
                {
                    "name": "prometheus-config",
                    "namespace": NAMESPACE,
                    "behavior": "replace",
                    "files": ["prometheus.yml", "alerts.yml"],
                }
            ]
        (path / "kustomization.yaml").write_text(json.dumps(overlay))
        return k8s.run([k8s.kubectl_path(), "kustomize", temporary], capture=True)


def check(extra_namespace=None):
    """Use the exact server image's promtool, before applying anything to Kubernetes."""
    (k8s.ROOT / "artifacts").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="promtool-", dir=k8s.ROOT / "artifacts") as temporary:
        path = Path(temporary)
        path.chmod(0o755)  # Let the nonroot promtool process traverse its read-only mount.
        (path / "prometheus.yml").write_text(configuration(extra_namespace))
        (path / "alerts.yml").write_text((BASE / "alerts.yml").read_text())
        # Validate the whole bundle: rule_files must resolve offline, too.
        command = [
            "docker",
            "--context",
            k8s.CONTEXT,
            "run",
            "--rm",
            "--network=none",
            "--entrypoint=/bin/promtool",
            "--mount",
            f"type=bind,source={path},target=/etc/prometheus,readonly",
            IMAGE,
        ]
        return k8s.run(
            command + ["check", "config", "/etc/prometheus/prometheus.yml"], capture=True
        ) + k8s.run(command + ["check", "rules", "/etc/prometheus/alerts.yml"], capture=True)


def diagnostics():
    for args in [
        ["get", "deployment,pods,pvc,svc", "-o", "wide"],
        ["get", "events", "--sort-by=.metadata.creationTimestamp"],
        ["describe", "pods", "-l", LABEL],
        ["logs", "deployment/prometheus", "--tail=100"],
    ]:
        k8s.kube(*args, namespace=NAMESPACE, check=False)


def deploy(extra_namespace=None):
    print(check(extra_namespace), end="")
    manifest = render(extra_namespace)
    # The existing application namespace is never created, modified, or redeployed here.
    k8s.kube("get", "namespace", k8s.NAMESPACE, capture=True)
    k8s.kube("apply", "-f", str(BASE / "namespace.yaml"))
    k8s.kube("apply", "--dry-run=server", "-f", "-", input=manifest)
    k8s.kube("apply", "-f", "-", input=manifest)
    rollout()


def rollout():
    try:
        k8s.kube(
            "rollout", "status", "deployment/prometheus", "--timeout=240s", namespace=NAMESPACE
        )
    except subprocess.CalledProcessError:
        diagnostics()
        raise


@contextmanager
def forward(namespace=NAMESPACE):
    """Temporary random localhost port; no conflict with a user's foreground port-forward."""
    with tempfile.TemporaryFile(mode="w+") as log:
        process = subprocess.Popen(
            [
                str(k8s.kubectl_path()),
                "--context",
                k8s.CONTEXT,
                "--namespace",
                namespace,
                "port-forward",
                "--address=127.0.0.1",
                "service/prometheus",
                ":9090",
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
                    raise RuntimeError(f"Prometheus port-forward exited: {output}")
                match = re.search(r"Forwarding from 127\.0\.0\.1:(\d+)", output)
                if match:
                    yield Client(f"http://127.0.0.1:{match[1]}")
                    return
                time.sleep(0.1)
            raise TimeoutError("Prometheus port-forward did not start within 20 seconds")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


class Client:
    def __init__(self, url):
        self.url = url
        self.queries = []

    def get(self, path, params=None):
        url = self.url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=10) as response:
            result = json.load(response)
        if result.get("status") != "success":
            raise RuntimeError(f"Prometheus API failed: {result}")
        return result["data"]

    def query(self, expression, at=None):
        params = {"query": expression}
        if at is not None:
            params["time"] = str(at)
        result = self.get("/api/v1/query", params)
        self.queries.append({"query": expression, "time": at, "data": result})
        return result["result"]

    def targets(self):
        return self.get("/api/v1/targets", {"state": "active"})["activeTargets"]


def status():
    k8s.kube("get", "deployment,pods,pvc,svc", "-o", "wide", namespace=NAMESPACE)
    with forward() as client:
        print(json.dumps(client.targets(), indent=2))


def remove_workload():
    k8s.kube(
        "delete",
        "deployment,service,configmap,serviceaccount",
        "-l",
        LABEL,
        "--ignore-not-found",
        namespace=NAMESPACE,
    )
    k8s.kube(
        "delete",
        "role,rolebinding",
        "prometheus-pod-reader",
        "--ignore-not-found",
        namespace=k8s.NAMESPACE,
    )
    print("Prometheus workload removed. Namespace and prometheus-data PVC retained.")


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
            "query",
            "validate",
            "remove-workload",
        ],
    )
    parser.add_argument("expression", nargs="?", help="PromQL expression for query")
    parser.add_argument("--time", help="Optional fixed query evaluation timestamp")
    args = parser.parse_args()
    if args.command == "check":
        print(check(), end="")
    elif args.command == "render":
        print(render(), end="")
    elif args.command == "deploy":
        deploy()
        print("Prometheus deployed. Run: python3 scripts/prometheus.py access")
    elif args.command == "status":
        status()
    elif args.command == "access":
        k8s.kube(
            "port-forward",
            "--address=127.0.0.1",
            "service/prometheus",
            "9090:9090",
            namespace=NAMESPACE,
        )
    elif args.command == "query":
        if not args.expression:
            parser.error("query requires a PromQL expression")
        with forward() as client:
            print(json.dumps(client.query(args.expression, args.time), indent=2))
    elif args.command == "validate":
        from prometheus_validate import validate

        validate()
    elif args.command == "remove-workload":
        remove_workload()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        sys.exit(f"Prometheus operation failed: {exc}")
