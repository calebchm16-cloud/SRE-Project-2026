"""Keep cloud identity, release transfer and published deployment evidence bounded."""

import ast
import importlib
import json
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import aws_lab
import publish_deployment as publication


def test_operational_helpers_parse_on_the_ubuntu_guest():
    for path in (aws_lab.ROOT / "scripts").glob("*.py"):
        ast.parse(path.read_text(), filename=str(path), feature_version=(3, 12))


@pytest.mark.parametrize("module_name", ["prometheus", "alertmanager", "alertmanager_rule_tests"])
def test_nonroot_validation_containers_can_read_mounted_fixtures(
    module_name, tmp_path, monkeypatch
):
    module = importlib.import_module(module_name)
    monkeypatch.setattr(module.k8s, "ROOT", tmp_path)
    mounts = []

    def run(arguments, **kwargs):
        for argument in arguments:
            if isinstance(argument, str) and argument.startswith("type=bind,source="):
                source = Path(argument.split("source=", 1)[1].split(",", 1)[0])
                if source.is_dir():
                    assert source.stat().st_mode & 0o005 == 0o005
                    mounts.append(source)
        return "SUCCESS"

    monkeypatch.setattr(module.k8s, "run", run)
    if module_name == "alertmanager_rule_tests":
        module.run()
    else:
        module.check()
    assert mounts


def test_alertmanager_bootstraps_its_namespace_before_server_validation(monkeypatch):
    module = importlib.import_module("alertmanager")
    prometheus = importlib.import_module("prometheus")
    calls = []
    monkeypatch.setattr(module.k8s, "kube", lambda *a, **kw: calls.append(a) or "")
    monkeypatch.setattr(module, "check", lambda *a: "")
    monkeypatch.setattr(module, "render", lambda *a: "manifest")
    monkeypatch.setattr(module, "rollout", lambda *a: None)
    monkeypatch.setattr(prometheus, "deploy", lambda: None)
    module.deploy()
    assert calls[0][:2] == ("apply", "-f")
    assert calls[0][2].endswith("monitoring/prometheus/namespace.yaml")
    assert calls[1][0] == "get"


def running():
    return {
        "verified": True,
        "status": "running",
        "public_ip": "203.0.113.10",
        "source_sha": "a" * 40,
        "verified_at": "2026-11-14T12:00:00+00:00",
        "paths": dict.fromkeys(publication.PATHS, 200),
        "private_account_id": "must-not-be-published",
        "password": "must-not-be-published",
    }


@pytest.mark.parametrize("github_actions", [True, False])
def test_terminal_updates_local_readme_while_actions_defers_publication(
    tmp_path, monkeypatch, github_actions
):
    monkeypatch.setattr(aws_lab, "ROOT", tmp_path)
    monkeypatch.setattr(aws_lab, "BASE", tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true" if github_actions else "false")
    readme = tmp_path / "README.md"
    original = publication.START + "\nold status\n" + publication.END
    readme.write_text(original)
    aws_lab.write_report(running())
    assert (tmp_path / "deployment.json").exists()
    if github_actions:
        assert readme.read_text() == original
    else:
        assert "http://203.0.113.10" in readme.read_text()


def test_readme_publishes_only_verified_allowlisted_fields_and_preserves_prose(tmp_path):
    path = tmp_path / "README.md"
    path.write_text("before\n" + publication.START + "\nold\n" + publication.END + "\nafter\n")
    publication.update(path, running(), "friend/cahuita-conditions")
    output = path.read_text()
    assert output.startswith("before\n") and output.endswith("\nafter\n")
    assert "http://203.0.113.10/docs" in output
    assert "must-not-be-published" not in output
    assert "not a live uptime check" in output


@pytest.mark.parametrize(
    "change",
    [
        {"verified": False},
        {"public_ip": "x\n<script>"},
        {"source_sha": "main; echo bad"},
        {"paths": {}},
    ],
)
def test_failed_or_injectable_release_cannot_be_published(change):
    with pytest.raises(ValueError):
        publication.section({**running(), **change}, "friend/cahuita-conditions")


@pytest.mark.parametrize("status", ["stopped", "destroyed"])
def test_teardown_removes_all_previous_ip_links(tmp_path, status):
    path = tmp_path / "README.md"
    path.write_text(publication.START + "\nold IP\n" + publication.END)
    publication.update(path, {**running(), "status": status}, "friend/cahuita-conditions")
    assert "203.0.113.10" not in path.read_text()
    assert "no active application link" in path.read_text()


def test_wrong_aws_account_stops_before_terraform(monkeypatch):
    monkeypatch.setattr(aws_lab, "settings", lambda: {"allowed_account_id": "123456789012"})
    monkeypatch.setattr(aws_lab, "aws", lambda *a: {"Account": "999999999999"})
    monkeypatch.setattr(aws_lab, "initialize", lambda: pytest.fail("Must not initialize Terraform"))
    with pytest.raises(RuntimeError, match="other than the dedicated"):
        aws_lab.plan(apply=True)


def test_terraform_replacement_is_blocked_in_normal_apply(monkeypatch):
    monkeypatch.setattr(aws_lab, "check_identity", lambda: None)
    monkeypatch.setattr(aws_lab, "initialize", lambda: None)
    calls = []

    def tf(*args, **kwargs):
        calls.append(args[0])
        return json.dumps({"resource_changes": [{"change": {"actions": ["delete", "create"]}}]})

    monkeypatch.setattr(aws_lab, "tf", tf)
    with pytest.raises(RuntimeError, match="delete/replace"):
        aws_lab.plan(apply=True)
    assert "apply" not in calls


def test_source_transfer_excludes_local_secrets_and_state(tmp_path, monkeypatch):
    monkeypatch.setattr(aws_lab, "ROOT", tmp_path)
    for name in aws_lab.SAFE_FILES:
        (tmp_path / name).write_text("source")
    for name in aws_lab.SAFE_DIRECTORIES:
        (tmp_path / name).mkdir()
    (tmp_path / "src/application.py").write_text("source")
    (tmp_path / ".env").write_text("a secret")
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts/state.tfstate").write_text("private")
    (tmp_path / "src/__pycache__").mkdir()
    (tmp_path / "src/__pycache__/cache.pyc").write_text("generated")
    target = tmp_path / "release.tar.gz"
    aws_lab.bundle(target)
    with tarfile.open(target) as archive:
        names = archive.getnames()
    assert "src/application.py" in names
    assert not any("artifacts" in n or "__pycache__" in n or n == ".env" for n in names)
