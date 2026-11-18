"""Guard demo isolation, interrupted cleanup and publishable evidence provenance offline."""

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import failure_demo as demo
import failure_demo_env as env
import failure_demo_publish as publisher


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(env, "BASE", tmp_path)
    result = {
        "run_id": "0123456789",
        "namespace": "cahuita-alert-validation-0123456789",
        "namespace_uid": "original-namespace",
        "phase": "healthy",
        "result": "incomplete",
        "cleanup": "pending",
        "before": {"production": "unchanged"},
        "phases": {},
    }
    env.directory(result["run_id"]).mkdir()
    env.save(result)
    return result


@pytest.mark.parametrize("error", [RuntimeError("provider/capture failed"), KeyboardInterrupt()])
def test_automatic_failure_always_cleans_and_marks_run_incomplete(monkeypatch, state, error):
    monkeypatch.setattr(demo, "start", lambda mode: state)
    cleaned = []

    def fail(*args):
        raise error

    monkeypatch.setattr(demo, "phase", fail)
    monkeypatch.setattr(demo, "cleanup", lambda value: cleaned.append(copy.deepcopy(value)))
    with pytest.raises(type(error)):
        demo.run()
    assert len(cleaned) == 1
    assert cleaned[0]["result"] in {"failed", "interrupted"}
    with pytest.raises(ValueError, match="successful, cleaned run"):
        publisher.validate(cleaned[0])


@pytest.mark.parametrize(
    "uid,label", [("recreated-namespace", "0123456789"), ("original-namespace", "another-owner")]
)
def test_cleanup_refuses_a_replaced_or_unowned_namespace(monkeypatch, state, uid, label):
    deleted = []

    def kube(*args, **kwargs):
        if args[0] == "delete":
            deleted.append(args)
        return state["namespace"]

    monkeypatch.setattr(env.k8s, "kube", kube)
    monkeypatch.setattr(
        env.k8s,
        "kube_json",
        lambda *a, **kw: {"metadata": {"uid": uid, "labels": {env.OWNER: label}}},
    )
    with pytest.raises(RuntimeError, match="ownership changed"):
        demo.cleanup(state)
    assert deleted == []
    assert state["cleanup"] == "pending"


def test_cleanup_is_idempotent_and_deletes_only_its_run(monkeypatch, state):
    mutations = []
    monkeypatch.setattr(env, "ownership", lambda value: None)
    monkeypatch.setattr(env.k8s, "kube", lambda *a, **kw: mutations.append(a) or "found")
    monkeypatch.setattr(env, "preserved_workloads", lambda: state["before"])
    demo.cleanup(state)
    demo.cleanup(state)
    assert [a for a in mutations if a[0] == "delete"] == [
        ("delete", "namespace", state["namespace"], "--timeout=120s")
    ]
    assert state["cleanup"] == "passed" and state["preserved"] is True


def test_manual_phase_cannot_skip_required_stages(monkeypatch, state):
    monkeypatch.setattr(env, "ownership", lambda value: None)
    with pytest.raises(ValueError, match="Expected partial before unavailable"):
        demo.phase(state, "unavailable")


@pytest.mark.parametrize("run_id", ["monitoring", "../01234567", "0123456789/..", "A123456789"])
def test_run_id_cannot_address_other_namespaces(run_id):
    with pytest.raises(ValueError, match="ten-character"):
        env.directory(run_id)


@pytest.fixture
def complete(state):
    state.update(
        {
            "result": "passed",
            "cleanup": "passed",
            "preserved": True,
            "created_at": "2026-11-03T12:00:00+00:00",
            "finished_at": "2026-11-03T12:06:00+00:00",
            "cached_download_at": "2026-11-03T12:00:00+00:00",
            "image": "cahuita-conditions:0.1.0-example",
            "timings": {"expire_seconds": 120},
            "before": {"secret_that_must_not_be_exported": "private-demo-value"},
        }
    )
    directory = env.directory(state["run_id"])
    (directory / "rule-activation.json").write_text(
        json.dumps(
            {
                "activated_at": state["created_at"],
                "startup_notifications": 0,
            }
        )
    )
    (directory / "screenshots").mkdir()
    for phase in env.PHASES:
        images = []
        for filename in env.CAPTURES[phase]:
            data = b"\x89PNG\r\n\x1a\n" + filename.encode()
            (directory / "screenshots" / filename).write_bytes(data)
            images.append(
                {
                    "file": "screenshots/" + filename,
                    "run_id": state["run_id"],
                    "phase": phase,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "captured_at": state["created_at"],
                    "origin": "test fixture",
                }
            )
        state["phases"][phase] = {"images": images, "snapshot": phase + ".json"}
        source = {
            "last_success_at": state["cached_download_at"],
            "hourly": [{}],
            "available_hours": 48,
            "freshness": "fresh",
            "cache_age_seconds": 1,
            "last_outcome": "success",
        }
        snapshot = {
            "run_id": state["run_id"],
            "namespace": state["namespace"],
            "phase": phase,
            "notification_channel": "simulated_telegram_api",
            "captured_at": state["created_at"],
            "responses": {
                "forecast": {
                    "status": 200,
                    "body": {
                        "status": "ok",
                        "weather": source,
                        "marine": source,
                    },
                },
                "ready": {"status": 200},
                "live": {"status": 200},
            },
            "kubernetes_ready": True,
            "firing": [],
            "messages": [
                {
                    "code": 200,
                    "text": (
                        ("RESOLVED" if phase == "recovered" else "FIRING")
                        + "\nAlert: CahuitaForecastsUnavailable\nNamespace: "
                        + state["namespace"]
                        + "\nLocation: cahuita\nStarted (UTC): 2026-11-03T12:04:00Z\n"
                        + ("Ended (UTC): 2026-11-03T12:05:00Z\n" if phase == "recovered" else "")
                    ),
                    "timestamp": 1793707440,
                }
            ],
        }
        if phase == "healthy":
            snapshot["messages"] = []
        (directory / (phase + ".json")).write_text(json.dumps(snapshot))
    return state


def test_publication_whitelists_output_and_all_links_resolve(complete, tmp_path):
    destination = tmp_path / "published"
    publisher.publish(complete, destination)
    files = list(destination.iterdir())
    assert len(list(destination.glob("*.png"))) == 12
    assert len(files) == 14
    assert "private-demo-value" not in "".join(
        path.read_text() for path in files if path.suffix != ".png"
    )
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["notification_channel"] == "simulated_telegram_api"
    for image in manifest["screenshots"]:
        assert (destination / image["file"]).is_file()


@pytest.mark.parametrize(
    "problem",
    [
        "missing-phase",
        "mixed-run",
        "changed-png",
        "no-resolve",
        "different-start",
        "delivery-500",
        "startup-warning",
    ],
)
def test_incomplete_or_mixed_evidence_is_rejected(complete, problem):
    directory = env.directory(complete["run_id"])
    if problem == "missing-phase":
        del complete["phases"]["partial"]
    elif problem == "mixed-run":
        complete["phases"]["cached"]["images"][0]["run_id"] = "9999999999"
    elif problem == "changed-png":
        path = directory / "screenshots/01-healthy-app.png"
        path.write_bytes(path.read_bytes() + b"modified")
    else:
        path = directory / ("healthy.json" if problem == "startup-warning" else "recovered.json")
        snapshot = json.loads(path.read_text())
        if problem == "no-resolve":
            snapshot["messages"] = []
        elif problem == "different-start":
            snapshot["messages"][0]["text"] = snapshot["messages"][0]["text"].replace(
                "12:04:00Z", "12:04:01Z"
            )
        elif problem == "startup-warning":
            snapshot["messages"] = [{"code": 200, "text": "FIRING startup"}]
        else:
            snapshot["messages"][0]["code"] = 500
        path.write_text(json.dumps(snapshot))
    with pytest.raises(ValueError):
        publisher.validate(complete)
