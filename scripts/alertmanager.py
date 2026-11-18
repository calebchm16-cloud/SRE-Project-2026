#!/usr/bin/env python3
"""Operate Alertmanager and its private Telegram channel in the Cahuita context."""

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import k8s_validate as fixture

import k8s

BASE = k8s.ROOT / "monitoring/alertmanager"
NAMESPACE = "monitoring"
LABEL = "app.kubernetes.io/name=alertmanager"
IMAGE = "prom/alertmanager:v0.34.2"
SECRET = "alertmanager-telegram"


def configuration(mock_url=None, telegram_enabled=True):
    text = (BASE / "alertmanager.yml").read_text()
    if not telegram_enabled:
        if mock_url:
            raise ValueError("A mock requires the Telegram integration to be enabled")
        text = text.replace("receiver: telegram", "receiver: unconfigured")
        before, remainder = text.split("receivers:\n", 1)
        before = before.split("  routes:\n", 1)[0]
        _, after = remainder.split("inhibit_rules:\n", 1)
        return before + "receivers:\n  - name: unconfigured\ninhibit_rules:\n" + after
    if mock_url:
        if not re.fullmatch(
            r"http://telegram-mock\.[a-z0-9-]+\.svc\.cluster\.local:9001", mock_url
        ):
            raise ValueError("Validation Telegram endpoint must be the isolated mock Service")
        text = text.replace("group_wait: 30s", "group_wait: 1s").replace(
            "group_interval: 1m", "group_interval: 10s"
        )
        text = text.replace("- bot_token_file:", f"- api_url: {mock_url}\n        bot_token_file:")
    return text


def render(namespace=NAMESPACE, mock_url=None, telegram_enabled=True):
    if namespace == NAMESPACE and mock_url:
        raise ValueError("A simulated receiver cannot replace production Telegram")
    if namespace == NAMESPACE and telegram_enabled:
        return k8s.run([k8s.kubectl_path(), "kustomize", BASE], capture=True)
    if namespace != NAMESPACE and not re.fullmatch(
        r"cahuita-alert-validation-[a-f0-9]{10}", namespace
    ):
        raise ValueError("Only owned validation namespaces are supported")
    (k8s.ROOT / "artifacts").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="alertmanager-kustomize-", dir=k8s.ROOT / "artifacts"
    ) as temp:
        path = Path(temp)
        (path / "alertmanager.yml").write_text(configuration(mock_url, telegram_enabled))
        (path / "notification.tmpl").write_text((BASE / "notification.tmpl").read_text())
        overlay = {
            "apiVersion": "kustomize.config.k8s.io/v1beta1",
            "kind": "Kustomization",
            "namespace": namespace,
            "resources": [os.path.relpath(BASE, path)],
            "configMapGenerator": [
                {
                    "name": "alertmanager-config",
                    "namespace": NAMESPACE,
                    "behavior": "replace",
                    "files": ["alertmanager.yml", "notification.tmpl"],
                }
            ],
        }
        if not telegram_enabled:
            overlay["patches"] = [
                {
                    "patch": json.dumps(
                        {
                            "apiVersion": "apps/v1",
                            "kind": "Deployment",
                            "metadata": {"name": "alertmanager"},
                            "spec": {
                                "template": {
                                    "spec": {
                                        "volumes": [
                                            {
                                                "name": "telegram",
                                                "secret": {"secretName": SECRET, "optional": True},
                                            }
                                        ]
                                    }
                                }
                            },
                        }
                    )
                }
            ]
        (path / "kustomization.yaml").write_text(json.dumps(overlay))
        return k8s.run([k8s.kubectl_path(), "kustomize", path], capture=True)


def check(mock_url=None, telegram_enabled=True):
    """Check exact image/config/templates offline using fake credential files."""
    (k8s.ROOT / "artifacts").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="amtool-", dir=k8s.ROOT / "artifacts") as temp:
        path = Path(temp)
        # The image runs as nobody; Linux preserves bind-mount directory permissions.
        # This directory contains configuration and deliberately fake test credentials.
        path.chmod(0o755)
        (path / "alertmanager.yml").write_text(configuration(mock_url, telegram_enabled))
        (path / "notification.tmpl").write_text((BASE / "notification.tmpl").read_text())
        (path / "bot-token").write_text("1:validation-only-token")
        (path / "chat-id").write_text("1")
        return k8s.run(
            [
                "docker",
                "--context",
                k8s.CONTEXT,
                "run",
                "--rm",
                "--network=none",
                "--entrypoint=/bin/amtool",
                "--mount",
                f"type=bind,source={path},target=/etc/alertmanager,readonly",
                "--mount",
                f"type=bind,source={path},target=/run/telegram,readonly",
                IMAGE,
                "check-config",
                "/etc/alertmanager/alertmanager.yml",
            ],
            capture=True,
        )


def telegram_request(token, method):
    """Never expose urllib's URL errors: Telegram embeds the token in its URL path."""
    url = "https://api.telegram.org/bot" + urllib.parse.quote(token, safe=":") + "/" + method
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=b""), timeout=15) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"Telegram rejected {method} (HTTP {exc.code}); verify the bot token"
        ) from None
    except (OSError, ValueError):
        raise RuntimeError(f"Telegram {method} failed; check connectivity and try again") from None
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} was not accepted")
    return data["result"]


def configure_telegram():
    if not sys.stdin.isatty():
        raise RuntimeError("Run configure-telegram in your local terminal for hidden token entry")
    token = getpass.getpass("Telegram bot token (hidden): ").strip()
    if not token or any(char.isspace() for char in token):
        raise ValueError("A nonempty token without whitespace is required")
    telegram_request(token, "getMe")
    if telegram_request(token, "getWebhookInfo").get("url"):
        raise RuntimeError("This bot has a webhook; use a dedicated new bot instead")
    updates = telegram_request(token, "getUpdates")
    chats = {
        message["chat"]["id"]: message["chat"]
        for update in updates
        if (message := update.get("message")) and message.get("chat", {}).get("type") == "private"
    }
    if not chats:
        raise RuntimeError(
            "Open your new bot in Telegram, send /start, then rerun configure-telegram"
        )
    candidates = list(chats.values())
    selected = 0
    if len(candidates) > 1:
        for index, chat in enumerate(candidates, 1):
            name = chat.get("username") or chat.get("first_name", "Private chat")
            print(f"{index}. {name} (chat {chat['id']})")
        selected = int(input("Choose the private destination number: ")) - 1
        if not 0 <= selected < len(candidates):
            raise ValueError("Unknown destination")
    existing = k8s.kube(
        "get",
        "secret",
        SECRET,
        "--ignore-not-found",
        "-o",
        "json",
        namespace=NAMESPACE,
        capture=True,
    )
    metadata = {"name": SECRET, "namespace": NAMESPACE}
    if existing.strip():
        metadata["resourceVersion"] = json.loads(existing)["metadata"]["resourceVersion"]
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": metadata,
        "stringData": {"bot-token": token, "chat-id": str(candidates[selected]["id"])},
    }
    action = "replace" if existing.strip() else "create"
    command = [str(k8s.kubectl_path()), "--context", k8s.CONTEXT, action, "-f", "-"]
    # No apply annotation, process arguments, temporary credential file or captured error body.
    result = subprocess.run(command, input=json.dumps(secret), text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError("Could not store the Telegram Secret; credentials were not logged")
    print("Telegram private destination configured. Token and chat ID were not printed.")
    print("Next: python3 scripts/alertmanager.py deploy")


def diagnostics(namespace=NAMESPACE):
    # Avoid raw notifier logs: third-party HTTP errors can include Telegram request URLs.
    k8s.kube(
        "get",
        "deployment,pods,pvc,svc",
        "-l",
        LABEL,
        "-o",
        "wide",
        namespace=namespace,
        check=False,
    )
    k8s.kube(
        "get", "events", "--sort-by=.metadata.creationTimestamp", namespace=namespace, check=False
    )


def rollout(namespace=NAMESPACE):
    try:
        k8s.kube(
            "rollout", "status", "deployment/alertmanager", "--timeout=240s", namespace=namespace
        )
    except subprocess.CalledProcessError:
        diagnostics(namespace)
        raise


def deploy(namespace=NAMESPACE, mock_url=None):
    if namespace == NAMESPACE:
        # A fresh cluster has no monitoring namespace yet; dry-run cannot create it.
        k8s.kube("apply", "-f", str(k8s.ROOT / "monitoring/prometheus/namespace.yaml"))
    secret = k8s.kube(
        "get",
        "secret",
        SECRET,
        "--ignore-not-found",
        "-o",
        "name",
        namespace=namespace,
        capture=True,
    )
    enabled = bool(secret.strip())
    if not enabled and namespace != NAMESPACE:
        raise RuntimeError("Validation requires its isolated fake Telegram Secret")
    if not enabled:
        print(
            "Telegram NOT CONFIGURED: local alert evaluation remains enabled; "
            "external delivery is disabled.",
            flush=True,
        )
    print(check(mock_url, enabled), end="", flush=True)
    manifest = render(namespace, mock_url, enabled)
    k8s.kube("apply", "--dry-run=server", "-f", "-", input=manifest)
    k8s.kube("apply", "-f", "-", input=manifest)
    rollout(namespace)
    if namespace == NAMESPACE:
        import prometheus

        prometheus.deploy()


@contextmanager
def forward(namespace=NAMESPACE, port=None):
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
                "service/alertmanager",
                f"{port or ''}:9093",
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
                    raise RuntimeError(f"Alertmanager port-forward exited: {output}")
                match = re.search(r"Forwarding from 127\.0\.0\.1:(\d+)", output)
                if match:
                    yield Client(f"http://127.0.0.1:{match[1]}")
                    return
                time.sleep(0.1)
            raise TimeoutError("Alertmanager port-forward did not start")
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

    def request(self, path, data=None, method=None, raw=False):
        req = urllib.request.Request(
            self.url + path,
            method=method,
            data=None if data is None else json.dumps(data).encode(),
            headers={} if data is None else {"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            body = response.read().decode()
        return body if raw else json.loads(body) if body else None

    def notifications(self, receiver=None):
        metrics = self.request("/metrics", raw=True)
        counters = {}
        for metric in (
            "alertmanager_notifications_total",
            "alertmanager_notifications_failed_total",
            "alertmanager_notification_requests_total",
            "alertmanager_notification_requests_failed_total",
        ):
            counters[metric] = sum(
                float(line.rsplit(" ", 1)[1])
                for line in metrics.splitlines()
                if line.startswith(metric + "{")
                and 'integration="telegram"' in line
                and (receiver is None or f'receiver_name="{receiver}"' in line)
            )
        return {
            "attempts": counters["alertmanager_notifications_total"],
            "failed": counters["alertmanager_notifications_failed_total"],
            # Logical attempts increment before sending. Requests increment after Notify
            # returns, so an in-flight/retrying request cannot count as accepted delivery.
            "completed_requests": counters["alertmanager_notification_requests_total"],
            "failed_requests": counters["alertmanager_notification_requests_failed_total"],
            "successful": counters["alertmanager_notification_requests_total"]
            - counters["alertmanager_notification_requests_failed_total"],
        }


def test_notification():
    """Send exactly one synthetic incident and resolution through the configured receiver."""
    stamp = datetime.now(UTC)
    labels = {
        "alertname": "CahuitaNotificationTest",
        "namespace": "notification-test-" + uuid.uuid4().hex[:8],
        "service": "cahuita-conditions",
        "severity": "info",
        "test": "true",
    }
    alert = {
        "labels": labels,
        "startsAt": stamp.isoformat(),
        "endsAt": (stamp + timedelta(minutes=10)).isoformat(),
        "annotations": {
            "summary": "TEST: Cahuita Telegram notification delivery",
            "description": "Synthetic notification test; no application failure is implied.",
            "action": "Verify this FIRING test message and its following RESOLVED message.",
        },
    }
    report = {"test": labels["namespace"], "started_at": stamp.isoformat()}
    with forward() as client:
        if "bot_token_file:" not in client.request("/api/v2/status")["config"]["original"]:
            raise RuntimeError(
                "Telegram is not configured. Run configure-telegram and deploy first"
            )
        before = client.notifications("telegram-test")
        try:
            client.request("/api/v2/alerts", [alert])
            print("Waiting for Telegram to accept the FIRING test notification...", flush=True)
            fixture.wait_for(
                "successful Telegram FIRING notification",
                lambda: client.notifications("telegram-test")["successful"] > before["successful"],
                timeout=150,
            )
            fired = client.notifications("telegram-test")
            alert["endsAt"] = datetime.now(UTC).isoformat()
            client.request("/api/v2/alerts", [alert])
            print("Waiting for Telegram to accept the RESOLVED test notification...", flush=True)
            fixture.wait_for(
                "successful Telegram RESOLVED notification",
                lambda: client.notifications("telegram-test")["successful"] > fired["successful"],
                timeout=150,
            )
            report.update(
                {
                    "result": "passed",
                    "before": before,
                    "after": client.notifications("telegram-test"),
                    "accepted": ["FIRING", "RESOLVED"],
                }
            )
        finally:
            alert["endsAt"] = datetime.now(UTC).isoformat()
            client.request("/api/v2/alerts", [alert])
    report["finished_at"] = datetime.now(UTC).isoformat()
    output = k8s.ROOT / "artifacts/alertmanager"
    output.mkdir(parents=True, exist_ok=True)
    (output / "telegram-delivery.json").write_text(json.dumps(report, indent=2) + "\n")
    print("Telegram accepted the test alert and recovery; no private message content was recorded.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "configure-telegram",
            "check",
            "render",
            "deploy",
            "status",
            "access",
            "validate",
            "test-notification",
            "remove-workload",
        ],
    )
    args = parser.parse_args()
    if args.command == "configure-telegram":
        configure_telegram()
    elif args.command == "check":
        print(check(), end="")
        import prometheus

        print(prometheus.check(), end="")
    elif args.command == "render":
        print(render(), end="")
    elif args.command == "deploy":
        deploy()
    elif args.command == "status":
        diagnostics()
        with forward() as client:
            state = client.request("/api/v2/status")
            print(
                json.dumps(
                    {
                        "version": state["versionInfo"]["version"],
                        "telegram_configured": "bot_token_file:" in state["config"]["original"],
                        "cluster": state["cluster"],
                        "alerts": client.request("/api/v2/alerts"),
                        "notifications": client.notifications(),
                    },
                    indent=2,
                )
            )
    elif args.command == "access":
        k8s.kube(
            "port-forward",
            "--address=127.0.0.1",
            "service/alertmanager",
            "9093:9093",
            namespace=NAMESPACE,
        )
    elif args.command == "test-notification":
        test_notification()
    elif args.command == "validate":
        from alertmanager_validate import validate

        validate()
    else:
        k8s.kube(
            "delete",
            "deployment,service,configmap",
            "-l",
            LABEL,
            "--ignore-not-found",
            namespace=NAMESPACE,
        )
        print("Alertmanager workload removed; its PVC and Telegram Secret are retained.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        sys.exit(f"Alertmanager operation failed: {exc}")
