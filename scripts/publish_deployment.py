#!/usr/bin/env python3
"""Update only the generated README block from sanitized, verified deployment evidence."""

import argparse
import ipaddress
import json
import re
from datetime import UTC, datetime
from pathlib import Path

START = "<!-- deployment:start -->"
END = "<!-- deployment:end -->"
PATHS = (
    "/",
    "/health/live",
    "/health/ready",
    "/api/v1/locations/cahuita/forecast",
    "/docs",
    "/openapi.json",
    "/metrics",
)


def section(report, repository=None):
    if repository and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Expected an owner/repository identifier")
    if report.get("verified") is not True:
        raise ValueError("An unverified run cannot update the published deployment")
    timestamp = datetime.fromisoformat(report["verified_at"])
    if timestamp.tzinfo is None:
        raise ValueError("Verification timestamp needs an explicit timezone")
    when = timestamp.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    badge = (
        f"[![CI](https://github.com/{repository}/actions/workflows/ci.yml/badge.svg)]"
        f"(https://github.com/{repository}/actions/workflows/ci.yml)"
    )
    lines = ["## Laboratory deployment", ""]
    if repository:
        lines += [badge, ""]
    status = report["status"]
    if status == "running":
        address = str(ipaddress.IPv4Address(report["public_ip"]))
        sha = report["source_sha"]
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("Expected a full verified commit SHA")
        if any(report.get("paths", {}).get(path) != 200 for path in PATHS):
            raise ValueError("All public endpoint checks must have passed")
        base = f"http://{address}"
        source = (
            f"[`{sha[:12]}`](https://github.com/{repository}/commit/{sha})"
            if repository
            else f"`{sha[:12]}`"
        )
        if report.get("local_changes"):
            source += " (local working tree with uncommitted changes)"
        lines += [
            f"Status: **Running when last verified** ({when}).",
            "",
            f"- Application: [{base}/]({base}/)",
            f"- API documentation: [{base}/docs]({base}/docs)",
            f"- Readiness: [{base}/health/ready]({base}/health/ready)",
            f"- Source: {source}",
            "",
            "Grafana, Prometheus and Alertmanager use private Systems Manager tunnels; "
            "see [AWS operation](docs/aws.md).",
            "",
            "This is an ephemeral, read-only HTTP demonstration. The address can change "
            "after stop/start. This timestamped snapshot is not a live uptime check.",
        ]
    elif status in {"stopped", "destroyed"}:
        lines += [
            f"Status: **{status.capitalize()}**, confirmed {when}.",
            "",
            "The laboratory has no active application link. Run the deployment workflow "
            "to provision or resume it and publish the current address.",
        ]
    else:
        raise ValueError("Unsupported deployment status")
    return "\n".join(lines)


def update(readme, report, repository=None):
    original = readme.read_text()
    if original.count(START) != 1 or original.count(END) != 1:
        raise ValueError("README must have exactly one deployment marker pair")
    before, rest = original.split(START, 1)
    _, after = rest.split(END, 1)
    readme.write_text(before + START + "\n" + section(report, repository) + "\n" + END + after)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--repository")
    parser.add_argument("--readme", type=Path, default=Path("README.md"))
    args = parser.parse_args()
    update(args.readme, json.loads(args.report.read_text()), args.repository)
