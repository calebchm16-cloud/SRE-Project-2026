"""Shared Kubernetes helpers for GitHub CI and the EC2 deployment."""
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = os.environ.get("CAHUITA_CONTEXT", "cahuita-server")
NAMESPACE = "cahuita-conditions"
KUBECTL_VERSION = "v1.35.0"
LABEL = "app.kubernetes.io/name=cahuita-conditions"


def kubectl_path():
    return Path(os.environ.get("KUBECTL_BIN", Path.home() / ".local/bin/kubectl"))


def run(args, *, capture=False, check=True, input=None, timeout=None):
    env = os.environ.copy()
    env["PATH"] = str(kubectl_path().parent) + os.pathsep + env.get("PATH", "")
    # Explicit contexts must win over a caller's unrelated Docker socket configuration.
    if args[0] == "docker":
        env.pop("DOCKER_HOST", None)
        env.pop("DOCKER_CONTEXT", None)
    try:
        result = subprocess.run(
            [str(arg) for arg in args],
            cwd=ROOT,
            env=env,
            input=input,
            text=True,
            capture_output=capture,
            check=check,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        if capture and exc.stderr:
            print(exc.stderr, file=sys.stderr, end="")
        raise
    return result.stdout if capture else result


def kube(*args, namespace=None, capture=False, check=True, input=None, timeout=None):
    command = [str(kubectl_path()), "--context", CONTEXT]
    if namespace:
        command += ["--namespace", namespace]
    return run(command + list(args), capture=capture, check=check, input=input, timeout=timeout)


def kube_json(*args, namespace=None):
    return json.loads(kube(*args, "-o", "json", namespace=namespace, capture=True))


def install_kubectl():
    target = kubectl_path()
    if target.exists():
        version = json.loads(run([target, "version", "--client", "-o", "json"], capture=True))
        if version["clientVersion"]["gitVersion"] != KUBECTL_VERSION:
            raise RuntimeError(f"Expected {KUBECTL_VERSION} at {target}; refusing to overwrite it")
        return
    architecture = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64"}[platform.machine()]
    system = {"Darwin": "darwin", "Linux": "linux"}[platform.system()]
    url = f"https://dl.k8s.io/release/{KUBECTL_VERSION}/bin/{system}/{architecture}/kubectl"
    expected = (
        urllib.request.urlopen(url + ".sha256", timeout=30).read().decode().strip().split()[0]
    )
    data = urllib.request.urlopen(url, timeout=90).read()
    if hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError("Official kubectl SHA256 verification failed")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="kubectl-cahuita-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
        os.chmod(temporary, 0o755)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print(f"Installed checksum-verified {KUBECTL_VERSION} at {target}")


def render(image, namespace=NAMESPACE, settings=None):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,61}[a-z0-9]", namespace):
        raise ValueError("Expected a DNS-compatible namespace")
    if "@" in image or ":" not in image or not re.fullmatch(r"[A-Za-z0-9./:_-]+", image):
        raise ValueError("Expected an explicitly tagged local image")
    name, tag = image.rsplit(":", 1)
    overlay = {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "namespace": namespace,
        "resources": [],
        "images": [{"name": "cahuita-conditions", "newName": name, "newTag": tag}],
    }
    if settings:
        overlay["configMapGenerator"] = [
            {
                "name": "cahuita-settings",
                "behavior": "merge",
                "literals": [f"{key}={value}" for key, value in settings.items()],
            }
        ]
    (ROOT / "artifacts").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="cahuita-kustomize-", dir=ROOT / "artifacts"
    ) as temporary:
        overlay["resources"] = [os.path.relpath(ROOT / "k8s", temporary)]
        (Path(temporary) / "kustomization.yaml").write_text(json.dumps(overlay))
        return run([kubectl_path(), "kustomize", temporary], capture=True)


def diagnostics(namespace=NAMESPACE):
    for args in [
        ["get", "pods,pvc,svc", "-o", "wide"],
        ["get", "events", "--sort-by=.metadata.creationTimestamp"],
        ["describe", "pods", "-l", LABEL],
        ["logs", "deployment/cahuita-conditions", "--all-containers", "--tail=80"],
    ]:
        kube(*args, namespace=namespace, check=False)


def apply(image, namespace=NAMESPACE, settings=None):
    # Create the namespace first so server-side dry-run can validate namespaced resources.
    kube(
        "apply",
        "-f",
        "-",
        input=json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": namespace},
            }
        ),
    )
    manifest = render(image, namespace, settings)
    kube("apply", "--dry-run=server", "-f", "-", input=manifest)
    kube("apply", "-f", "-", input=manifest)


def rollout(namespace=NAMESPACE):
    try:
        kube(
            "rollout",
            "status",
            "deployment/cahuita-conditions",
            "--timeout=240s",
            namespace=namespace,
        )
    except subprocess.CalledProcessError:
        diagnostics(namespace)
        raise
