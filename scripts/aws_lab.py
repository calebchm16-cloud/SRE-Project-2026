#!/usr/bin/env python3
"""Portable AWS laboratory lifecycle. Needs Python 3.11+, AWS CLI v2 and Terraform."""

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "artifacts/aws"
TF = BASE / "terraform"
SOURCE = ROOT / "infrastructure/terraform"
SAFE_DIRECTORIES = ("src", "scripts", "config", "k8s", "monitoring", "deployment")
SAFE_FILES = (
    "Dockerfile",
    ".dockerignore",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "LICENSE.md",
    "CONTRIBUTING.md",
)


def execute(args, *, env=None, capture=True):
    result = subprocess.run(
        [str(a) for a in args], cwd=ROOT, env=env, text=True, capture_output=capture, check=True
    )
    return result.stdout if capture else ""


def settings():
    path = BASE / "lab.json"
    result = json.loads(path.read_text()) if path.exists() else {}
    result["allowed_account_id"] = os.environ.get(
        "TF_VAR_allowed_account_id", result.get("allowed_account_id", "")
    )
    result["region"] = os.environ.get("AWS_REGION", result.get("region", "us-east-1"))
    result["project"] = os.environ.get("TF_VAR_project", result.get("project", "cahuita-lab"))
    return result


def environment():
    config = settings()
    if not re.fullmatch(r"[0-9]{12}", config["allowed_account_id"]):
        raise ValueError("Set TF_VAR_allowed_account_id to the dedicated laboratory account")
    env = os.environ.copy()
    env.update(
        AWS_PAGER="",
        AWS_REGION=config["region"],
        AWS_DEFAULT_REGION=config["region"],
        TF_VAR_allowed_account_id=config["allowed_account_id"],
        TF_VAR_region=config["region"],
        TF_VAR_project=config["project"],
        TF_IN_AUTOMATION="1",
    )
    if config.get("profile") and not env.get("AWS_ACCESS_KEY_ID"):
        env["AWS_PROFILE"] = config["profile"]
    return env


def aws(*args):
    return json.loads(
        execute(["aws", *args, "--output", "json", "--no-cli-pager"], env=environment()) or "{}"
    )


def check_identity():
    identity = aws("sts", "get-caller-identity")
    if identity["Account"] != settings()["allowed_account_id"]:
        raise RuntimeError("Refusing to use an AWS account other than the dedicated lab")
    print("Verified the configured laboratory account (identity is not published).")


def initialize():
    TF.mkdir(parents=True, exist_ok=True)
    for path in [*SOURCE.glob("*.tf"), SOURCE / "bootstrap.sh", SOURCE / ".terraform.lock.hcl"]:
        shutil.copyfile(path, TF / path.name)
    backend = TF / "backend.tf"
    bucket = os.environ.get("CAHUITA_TF_STATE_BUCKET", settings().get("state_bucket"))
    if bucket:
        backend.write_text(
            json.dumps(
                {
                    "terraform": {
                        "backend": {
                            "s3": {
                                "bucket": bucket,
                                "key": settings()["project"] + "/terraform.tfstate",
                                "region": settings()["region"],
                                "encrypt": True,
                                "use_lockfile": True,
                            }
                        }
                    }
                }
            )
        )
        # JSON is valid HCL only with the .tf.json extension.
        backend.rename(TF / "backend.tf.json")
    elif os.environ.get("GITHUB_ACTIONS") == "true":
        raise RuntimeError("Actions requires CAHUITA_TF_STATE_BUCKET for persistent private state")
    elif (TF / "backend.tf.json").exists():
        raise RuntimeError("Supply the same CAHUITA_TF_STATE_BUCKET as the existing deployment")
    execute(
        ["terraform", f"-chdir={TF}", "init", "-input=false", "-no-color"],
        env=environment(),
        capture=False,
    )


def tf(*args, capture=True):
    return execute(["terraform", f"-chdir={TF}", *args], env=environment(), capture=capture)


def bootstrap_state():
    """One-time private backend; retained separately from disposable lab resources."""
    if os.environ.get("CAHUITA_TF_STATE_BUCKET") or settings().get("state_bucket"):
        return
    if os.environ.get("GITHUB_ACTIONS") == "true":
        raise RuntimeError("Configure the private state bucket before running Actions")
    check_identity()
    path = BASE / "state-bootstrap"
    path.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "infrastructure/state/main.tf", path / "main.tf")
    for command in (
        ["init", "-input=false", "-no-color"],
        ["apply", "-auto-approve", "-input=false", "-no-color"],
    ):
        execute(["terraform", f"-chdir={path}", *command], env=environment(), capture=False)
    bucket = execute(
        ["terraform", f"-chdir={path}", "output", "-raw", "bucket"], env=environment()
    ).strip()
    config = settings()
    config["state_bucket"] = bucket
    target = BASE / "lab.json"
    target.write_text(json.dumps(config, indent=2) + "\n")
    target.chmod(0o600)


def outputs():
    return {key: value["value"] for key, value in json.loads(tf("output", "-json")).items()}


def write_report(report):
    (BASE / "deployment.json").write_text(json.dumps(report, indent=2) + "\n")
    if os.environ.get("GITHUB_ACTIONS") != "true":
        # Terminal operations update the local README without committing or pushing it.
        from publish_deployment import update

        update(ROOT / "README.md", report)


def plan(apply=False):
    check_identity()
    initialize()
    tf("plan", "-input=false", "-no-color", "-out=lab.tfplan", capture=False)
    report = json.loads(tf("show", "-json", "lab.tfplan"))
    if any(
        "delete" in change["change"]["actions"] for change in report.get("resource_changes", [])
    ):
        raise RuntimeError(
            "Plan would delete/replace resources; review it separately before applying"
        )
    if apply:
        tf("apply", "-input=false", "-no-color", "lab.tfplan", capture=False)
        print(json.dumps(outputs(), indent=2))


def bundle(path):
    """Allowlist source inputs. Never traverse .git, artifacts, .env or Terraform state."""
    with tarfile.open(path, "w:gz") as archive:
        files = [ROOT / name for name in SAFE_FILES]
        for directory in SAFE_DIRECTORIES:
            files.extend((ROOT / directory).rglob("*"))
        for file in sorted(set(files)):
            if not file.is_file() or "__pycache__" in file.parts or file.suffix == ".pyc":
                continue
            if file.is_symlink():
                raise ValueError("Release input must not be a symlink")
            info = archive.gettarinfo(str(file), arcname=str(file.relative_to(ROOT)))
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with file.open("rb") as data:
                archive.addfile(info, data)


def digest(path):
    with path.open("rb") as data:
        return hashlib.file_digest(data, "sha256").hexdigest()


def upload(bucket, key, path):
    execute(
        ["aws", "s3", "cp", path, f"s3://{bucket}/{key}", "--only-show-errors"], env=environment()
    )


def owned_instance(instance_id):
    response = aws("ec2", "describe-instances", "--instance-ids", instance_id)
    instance = response["Reservations"][0]["Instances"][0]
    tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
    if tags.get("Project") != settings()["project"] or tags.get("ManagedBy") != "Terraform":
        raise RuntimeError("Refusing to operate on an instance outside this laboratory")
    return instance


def wait_ssm(instance_id):
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        result = aws(
            "ssm",
            "describe-instance-information",
            "--filters",
            json.dumps(
                [
                    {"Key": "InstanceIds", "Values": [instance_id]},
                ]
            ),
        )
        if any(item["PingStatus"] == "Online" for item in result["InstanceInformationList"]):
            return
        time.sleep(5)
    raise RuntimeError("The instance did not register with Systems Manager")


def remote(instance_id, commands, *, timeout=1800):
    response = aws(
        "ssm",
        "send-command",
        "--instance-ids",
        instance_id,
        "--document-name",
        "AWS-RunShellScript",
        "--parameters",
        json.dumps(
            {
                "commands": commands,
                "executionTimeout": [str(timeout)],
            }
        ),
    )
    command_id = response["Command"]["CommandId"]
    deadline = time.monotonic() + timeout + 60
    while time.monotonic() < deadline:
        time.sleep(3)
        try:
            result = aws(
                "ssm",
                "get-command-invocation",
                "--command-id",
                command_id,
                "--instance-id",
                instance_id,
            )
        except subprocess.CalledProcessError as error:
            if "InvocationDoesNotExist" in (error.stderr or ""):
                continue
            raise
        if result["Status"] == "Success":
            return result["StandardOutputContent"]
        if result["Status"] not in {"Pending", "InProgress", "Delayed"}:
            # Never publish this remote output automatically: it is an administrative log.
            (BASE / "remote-failure.log").write_text(
                result["StandardOutputContent"] + "\n" + result["StandardErrorContent"]
            )
            raise RuntimeError(
                "Remote configuration failed; inspect artifacts/aws/remote-failure.log"
            )
    raise RuntimeError("Timed out waiting for Systems Manager; inspect the command in AWS")


def deploy(image_archive, image_tag, source_sha):
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("Supply the full tested Git commit SHA")
    if not re.fullmatch(r"cahuita-conditions:[a-zA-Z0-9_.-]+", image_tag):
        raise ValueError("Supply an explicitly tagged Cahuita image")
    check_identity()
    initialize()
    out = outputs()
    owned_instance(out["instance_id"])
    wait_ssm(out["instance_id"])
    release = BASE / "source.tar.gz"
    bundle(release)
    image_digest = digest(image_archive) if image_archive else "native-build"
    transfer_id = hashlib.sha256((digest(release) + image_digest).encode()).hexdigest()[:16]
    prefix = "releases/" + source_sha + "/" + transfer_id + "/"
    upload(out["release_bucket"], prefix + "source.tar.gz", release)
    variables = {"image_tag": image_tag, "source_sha": source_sha}
    names = ["source.tar.gz"]
    if image_archive:
        upload(out["release_bucket"], prefix + "image.tar", image_archive)
        variables["image_sha256"] = image_digest
        names.append("image.tar")
    script = [
        "set -eu",
        "cloud-init status --wait >/dev/null",
        "install -d -m 700 /opt/cahuita /opt/cahuita/releases/" + source_sha,
        "exec 9>/opt/cahuita/deploy.lock",
        "flock -n 9 || { echo 'Another deployment is running'; exit 1; }",
    ]
    for name in names:
        code = (
            "import boto3; boto3.client('s3').download_file("
            + repr(out["release_bucket"])
            + ","
            + repr(prefix + name)
            + ","
            + repr("/opt/cahuita/" + name)
            + ")"
        )
        script.append("/opt/cahuita-ansible/bin/python -c " + shlex.quote(code))
    script.extend(
        [
            f"echo '{digest(release)}  /opt/cahuita/source.tar.gz' | sha256sum --check --status",
            f"tar -xzf /opt/cahuita/source.tar.gz -C /opt/cahuita/releases/{source_sha}",
            f"ln -sfn /opt/cahuita/releases/{source_sha} /opt/cahuita/release",
            "cd /opt/cahuita/release",
            "/opt/cahuita-ansible/bin/ansible-playbook -i localhost, deployment/ansible/site.yml "
            "--extra-vars " + shlex.quote(json.dumps(variables)),
        ]
    )
    remote(out["instance_id"], script)
    evidence = json.loads(remote(out["instance_id"], ["cat /opt/cahuita/verification.json"]))
    if evidence.get("verified") is not True or evidence.get("source_sha") != source_sha:
        raise RuntimeError("Guest verification does not match this release")
    verify(out, source_sha)


def verify(out, source_sha):
    ipaddress.IPv4Address(out["public_ip"])
    paths = (
        "/",
        "/health/live",
        "/health/ready",
        "/api/v1/locations/cahuita/forecast",
        "/docs",
        "/openapi.json",
        "/metrics",
    )
    for path in paths:
        with urllib.request.urlopen(out["application_url"] + path, timeout=15) as response:
            if response.status != 200:
                raise RuntimeError("Public application verification failed")
    evidence = {
        "verified": True,
        "status": "running",
        "public_ip": out["public_ip"],
        "source_sha": source_sha,
        "verified_at": datetime.now(UTC).isoformat(),
        "paths": {path: 200 for path in paths},
        "local_changes": bool(
            execute(
                [
                    "git",
                    "status",
                    "--porcelain",
                    "--",
                    *SAFE_FILES,
                    *SAFE_DIRECTORIES,
                    "infrastructure",
                    ".github",
                ]
            ).strip()
        ),
    }
    write_report(evidence)
    print(json.dumps(evidence, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "configure",
            "up",
            "status",
            "bootstrap-state",
            "plan",
            "apply",
            "output",
            "deploy",
            "verify",
            "stop",
            "start",
            "destroy",
            "access",
        ],
    )
    parser.add_argument("--image-archive", type=Path)
    parser.add_argument("--image-tag")
    parser.add_argument("--source-sha")
    parser.add_argument("--profile")
    parser.add_argument("--account-id")
    parser.add_argument("--region")
    parser.add_argument(
        "--service", choices=["grafana", "prometheus", "alertmanager"], default="grafana"
    )
    args = parser.parse_args(argv)
    BASE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.command == "configure":
        if not args.profile or not re.fullmatch(r"[0-9]{12}", args.account_id or ""):
            parser.error("configure requires --profile and a twelve-digit --account-id")
        config = {
            "profile": args.profile,
            "allowed_account_id": args.account_id,
            "region": args.region or "us-east-1",
        }
        target = BASE / "lab.json"
        if target.exists():
            raise ValueError(
                "A lab configuration exists; edit it privately to preserve its backend"
            )
        target.write_text(json.dumps(config, indent=2) + "\n")
        target.chmod(0o600)
        print(
            "Saved private lab configuration. Authenticate with aws sso login --profile <profile>."
        )
        return
    if args.profile or args.account_id or args.region:
        parser.error("--profile, --account-id and --region are only used by configure")
    if args.command == "bootstrap-state":
        bootstrap_state()
        return
    if args.command == "up":
        if args.image_archive and not all([args.image_tag, args.source_sha]):
            parser.error("A transferred image requires --image-tag and --source-sha")
        sha = args.source_sha or execute(["git", "rev-parse", "HEAD"]).strip()
        image = args.image_tag or "cahuita-conditions:local-" + sha[:12]
        bootstrap_state()
        plan(apply=True)
        deploy(args.image_archive, image, sha)
        return
    if args.command in {"plan", "apply"}:
        if args.command == "apply":
            bootstrap_state()
        plan(apply=args.command == "apply")
        return
    if args.command == "deploy":
        if not all([args.image_archive, args.image_tag, args.source_sha]):
            parser.error("deploy requires --image-archive, --image-tag and --source-sha")
        deploy(args.image_archive, args.image_tag, args.source_sha)
        return
    check_identity()
    initialize()
    out = outputs()
    if args.command in {"output", "status"}:
        # Refresh the auto-assigned address; output alone never refreshes Terraform state.
        tf("apply", "-refresh-only", "-auto-approve", "-input=false", "-no-color", capture=False)
        print(json.dumps(outputs(), indent=2))
    elif args.command == "destroy":
        tf("destroy", "-auto-approve", "-input=false", "-no-color", capture=False)
        write_report(
            {"verified": True, "status": "destroyed", "verified_at": datetime.now(UTC).isoformat()}
        )
    elif args.command == "verify":
        if not args.source_sha:
            parser.error("verify requires --source-sha")
        verify(out, args.source_sha)
    else:
        owned_instance(out["instance_id"])
        if args.command in {"stop", "start"}:
            operation = "stop-instances" if args.command == "stop" else "start-instances"
            aws("ec2", operation, "--instance-ids", out["instance_id"])
            state = "instance-stopped" if args.command == "stop" else "instance-running"
            execute(
                ["aws", "ec2", "wait", state, "--instance-ids", out["instance_id"]],
                env=environment(),
            )
            tf(
                "apply",
                "-refresh-only",
                "-auto-approve",
                "-input=false",
                "-no-color",
                capture=False,
            )
            if args.command == "start":
                wait_ssm(out["instance_id"])
                source = json.loads(
                    remote(out["instance_id"], ["cat /opt/cahuita/verification.json"])
                )["source_sha"]
                latest = outputs()
                deadline = time.monotonic() + 180
                while True:
                    try:
                        verify(latest, source)
                        break
                    except urllib.error.URLError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(5)
            else:
                write_report(
                    {
                        "verified": True,
                        "status": "stopped",
                        "verified_at": datetime.now(UTC).isoformat(),
                    }
                )
        elif args.command == "access":
            port = {"grafana": 3000, "prometheus": 9090, "alertmanager": 9093}[args.service]
            execute(
                [
                    "aws",
                    "ssm",
                    "start-session",
                    "--target",
                    out["instance_id"],
                    "--document-name",
                    "AWS-StartPortForwardingSession",
                    "--parameters",
                    json.dumps({"portNumber": [str(port)], "localPortNumber": [str(port)]}),
                ],
                env=environment(),
                capture=False,
            )


if __name__ == "__main__":
    try:
        main()
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.CalledProcessError,
        urllib.error.URLError,
    ) as error:
        raise SystemExit(f"AWS laboratory error: {error}") from None
