"""Credential handling must remain safe even when Telegram returns URL-bearing errors."""

import io
import sys
import urllib.error
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import alertmanager


@pytest.mark.parametrize("http_error", [False, True])
def test_telegram_transport_error_does_not_expose_token(monkeypatch, http_error):
    token = "123456:private-validation-credential"

    def fail(request, **kwargs):
        assert token in request.full_url
        if http_error:
            raise urllib.error.HTTPError(request.full_url, 401, token, {}, None)
        raise urllib.error.URLError("Transport failed for " + request.full_url)

    monkeypatch.setattr(alertmanager.urllib.request, "urlopen", fail)
    with pytest.raises(RuntimeError) as caught:
        alertmanager.telegram_request(token, "getMe")
    assert token not in str(caught.value)
    assert "api.telegram.org" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_telegram_error_body_is_not_reflected(monkeypatch):
    token = "123456:private-validation-credential"
    monkeypatch.setattr(
        alertmanager.urllib.request,
        "urlopen",
        lambda *args, **kwargs: io.BytesIO(('{"ok":false,"description":"' + token + '"}').encode()),
    )
    with pytest.raises(RuntimeError) as caught:
        alertmanager.telegram_request(token, "getMe")
    assert token not in str(caught.value)


def test_simulated_receiver_cannot_replace_production():
    with pytest.raises(ValueError, match="cannot replace production"):
        alertmanager.render(mock_url="http://telegram-mock.example.svc.cluster.local:9001")


def test_real_delivery_test_rejects_unconfigured_receiver(monkeypatch):
    class Unconfigured:
        def request(self, path):
            assert path == "/api/v2/status"
            return {"config": {"original": "route:\n  receiver: unconfigured"}}

    @contextmanager
    def forward():
        yield Unconfigured()

    monkeypatch.setattr(alertmanager, "forward", forward)
    with pytest.raises(RuntimeError, match="not configured"):
        alertmanager.test_notification()


@pytest.mark.parametrize("requests,failed,successful", [(0, 0, 0), (2, 2, 0), (3, 2, 1)])
def test_delivery_requires_a_completed_successful_request(
    monkeypatch, requests, failed, successful
):
    client = alertmanager.Client("http://validation.invalid")
    metrics = (
        'alertmanager_notifications_total{integration="telegram"} 1\n'
        'alertmanager_notifications_failed_total{integration="telegram",reason="error"} 0\n'
        f'alertmanager_notification_requests_total{{integration="telegram"}} {requests}\n'
        f'alertmanager_notification_requests_failed_total{{integration="telegram"}} {failed}\n'
        'alertmanager_notification_requests_total{integration="email"} 100\n'
    )
    monkeypatch.setattr(client, "request", lambda *args, **kwargs: metrics)
    assert client.notifications()["successful"] == successful


def test_another_receiver_cannot_pass_the_real_delivery_test(monkeypatch):
    client = alertmanager.Client("http://validation.invalid")
    metrics = (
        'alertmanager_notification_requests_total{integration="telegram",'
        'receiver_name="telegram"} 30\n'
        'alertmanager_notification_requests_total{integration="telegram",'
        'receiver_name="telegram-test"} 0\n'
    )
    monkeypatch.setattr(client, "request", lambda *args, **kwargs: metrics)
    assert client.notifications("telegram-test")["successful"] == 0
