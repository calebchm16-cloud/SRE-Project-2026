#!/usr/bin/env python3
"""Configure the existing manifests on EC2; invoked locally there by Ansible."""

import argparse
import json
import os
import time
import urllib.request
from pathlib import Path

# No workstation lifecycle or browser dependencies on the guest.
os.environ["CAHUITA_CONTEXT"] = "cahuita-server"
os.environ["KUBECTL_BIN"] = "/usr/local/bin/kubectl"
os.environ["KUBECONFIG"] = "/etc/cahuita/kubeconfig.yaml"

import alertmanager  # noqa: E402
import grafana  # noqa: E402
import prometheus  # noqa: E402

import k8s  # noqa: E402


def deploy(image, source_sha):
    details = json.loads(k8s.run(["docker", "image", "inspect", image], capture=True))[0]
    if details["Config"].get("Labels", {}).get("org.opencontainers.image.revision") != source_sha:
        raise RuntimeError("Image revision differs from the verified source commit")
    context = k8s.run([k8s.kubectl_path(), "config", "current-context"], capture=True).strip()
    if context == "default":
        k8s.run([k8s.kubectl_path(), "config", "rename-context", "default", k8s.CONTEXT])
    elif context != k8s.CONTEXT:
        raise RuntimeError("Unexpected Kubernetes context on the laboratory node")
    # Docker emits one JSON object per line, rather than an array.
    contexts = [
        json.loads(row)
        for row in k8s.run(
            ["docker", "context", "ls", "--format", "json"], capture=True
        ).splitlines()
    ]
    if not any(row["Name"] == k8s.CONTEXT for row in contexts):
        k8s.run(
            [
                "docker",
                "context",
                "create",
                k8s.CONTEXT,
                "--docker",
                "host=unix:///var/run/docker.sock",
            ]
        )
    k8s.kube("wait", "--for=condition=Ready", "nodes", "--all", "--timeout=180s")
    k8s.kube(
        "wait",
        "--for=condition=Ready",
        "pods",
        "-l",
        "k8s-app=kube-dns",
        "--timeout=180s",
        namespace="kube-system",
    )
    node = k8s.kube_json("get", "nodes")["items"]
    if len(node) != 1 or node[0]["status"]["nodeInfo"]["architecture"] != details["Architecture"]:
        raise RuntimeError("The build architecture differs from the Kubernetes node")
    previous = k8s.kube(
        "get",
        "deployment",
        "cahuita-conditions",
        "--ignore-not-found",
        "-o",
        "json",
        namespace=k8s.NAMESPACE,
        capture=True,
    )
    k8s.apply(image, settings={"CAHUITA_REFRESH_SECONDS": "30"})
    if (
        previous
        and json.loads(previous)["spec"]["template"]["spec"]["containers"][0]["image"] == image
    ):
        k8s.kube("rollout", "restart", "deployment/cahuita-conditions", namespace=k8s.NAMESPACE)
    k8s.kube(
        "patch",
        "service",
        "cahuita-conditions",
        "--type=merge",
        "-p",
        json.dumps(
            {
                "spec": {
                    "type": "NodePort",
                    "ports": [
                        {
                            "name": "http",
                            "port": 8000,
                            "targetPort": "http",
                            "nodePort": 30080,
                        }
                    ],
                },
            }
        ),
        namespace=k8s.NAMESPACE,
    )
    k8s.rollout()
    running = k8s.kube_json("get", "pods", "-l", k8s.LABEL, namespace=k8s.NAMESPACE)["items"]
    if len(running) != 1 or not running[0]["status"]["containerStatuses"][0]["imageID"].endswith(
        details["Id"]
    ):
        raise RuntimeError("The running application does not match the transferred image")
    alertmanager.deploy()  # Includes Prometheus and its read-only discovery permissions.
    # Cold image pulls and SQLite migrations need extra time on a burstable lab VM.
    grafana.deploy(rollout_timeout="600s", startup_failure_threshold=150)
    with grafana.forward() as client:
        if client.request("/api/health")["database"] != "ok":
            raise RuntimeError("Grafana storage is unhealthy")
        if client.request("/api/datasources/uid/cahuita-prometheus/health")["status"] != "OK":
            raise RuntimeError("Grafana cannot query the provisioned Prometheus datasource")
        for uid in grafana.DASHBOARDS:
            if not client.request("/api/dashboards/uid/" + uid)["meta"]["provisioned"]:
                raise RuntimeError("A Grafana dashboard is not provisioned")
    with prometheus.forward() as client:
        for _ in range(24):
            targets = client.query('up{job=~"cahuita|prometheus|alertmanager"}')
            if len(targets) == 3 and all(float(row["value"][1]) == 1 for row in targets):
                break
            time.sleep(5)
        else:
            raise RuntimeError("The three Prometheus targets did not become UP")
    with urllib.request.urlopen("http://127.0.0.1:30080/health/ready", timeout=10) as response:
        if response.status != 200:
            raise RuntimeError("The forecast is not usable")
    evidence = {
        "verified": True,
        "source_sha": source_sha,
        "image": image,
        "image_id": details["Id"],
        "prometheus_targets_up": 3,
        "refresh_seconds": 30,
        "grafana_dashboards": list(grafana.DASHBOARDS),
    }
    path = Path("/opt/cahuita/verification.json")
    path.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--source-sha", required=True)
    args = parser.parse_args()
    deploy(args.image, args.source_sha)
