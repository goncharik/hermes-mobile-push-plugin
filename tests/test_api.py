"""Tests for the hermes-push REST routes (Task B2).

Mounts the plugin ``router`` on a throwaway FastAPI app and drives it with
TestClient, with the module store pointed at a tmp dir.

Auth note: the routes carry NO in-router auth because the host's dashboard
middleware gates ``/api/plugins/*`` (confirmed in B1/B2 against hermes-agent's
``auth_middleware`` + ``public_paths``). That middleware is part of the host
web-server app, not the plugin router, so there is nothing auth-related to
exercise here — these tests verify the route + store behaviour the router owns.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import json

from hermes_push import api
from hermes_push.sender import GatewaySender, HttpResponse
from hermes_push.store import TokenStore


class _FakeHttp:
    """Routes POSTs by URL so /test can be driven without a network.

    ``/register`` returns a scripted capability (or raises to simulate a failed
    capability fetch); ``/push`` returns a scripted status.
    """

    def __init__(self, *, register=None, push=None, capability="cap-test"):
        self._register = list(register or [])
        self._push = list(push or [])
        self._capability = capability
        self.register_calls = []
        self.push_calls = []

    def post_json(self, url, body, *, timeout):
        parsed = json.loads(body.decode("utf-8"))
        if url.endswith("/register"):
            self.register_calls.append(parsed)
            item = (
                self._register.pop(0)
                if self._register
                else HttpResponse(status=200, body=json.dumps({"capability": self._capability}))
            )
        else:
            self.push_calls.append(parsed)
            item = self._push.pop(0) if self._push else HttpResponse(status=200, body="")
        if isinstance(item, Exception):
            raise item
        return item


def _wire_sender(store, http):
    """A real GatewaySender over the store, with no real sleeping/threads."""
    return GatewaySender(
        store=store,
        gateway_url="https://gw.example/push",
        http_client=http,
        max_attempts=1,
        backoff_base_s=0.0,
        sleep=lambda _s: None,
    )


@pytest.fixture
def client(tmp_path):
    store = TokenStore(base_dir=tmp_path / "hermes-push")
    api.set_store(store)
    app = FastAPI()
    app.include_router(api.router)
    test_client = TestClient(app)
    yield test_client, store
    api.set_store(None) if hasattr(api, "set_store") else None
    api._store = None  # reset module global between tests
    api._sender = None  # reset the /test sender between tests


def test_register_persists_and_returns_no_secret(client):
    c, store = client
    resp = c.post(
        "/register",
        json={"device_token": "tok-1", "apns_env": "sandbox", "app_version": "1.2.3"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["device_token"] == "tok-1"
    assert body["apns_env"] == "sandbox"
    # The app never signs pushes, so no secret is minted or returned.
    assert "hmac_secret" not in body

    stored = store.get("tok-1")
    assert stored is not None
    assert "hmac_secret" not in stored
    assert stored["app_version"] == "1.2.3"


def test_reregister_same_token_updates_env_version(client):
    c, store = client
    c.post(
        "/register",
        json={"device_token": "tok-1", "apns_env": "sandbox", "app_version": "1.0.0"},
    )

    second = c.post(
        "/register",
        json={"device_token": "tok-1", "apns_env": "production", "app_version": "2.0.0"},
    ).json()

    assert second["apns_env"] == "production"
    assert store.get("tok-1")["app_version"] == "2.0.0"


def test_register_rejects_bad_apns_env(client):
    c, _ = client
    resp = c.post(
        "/register",
        json={"device_token": "tok", "apns_env": "staging", "app_version": "1"},
    )
    assert resp.status_code == 422  # pydantic validation


def test_register_rejects_missing_fields(client):
    c, _ = client
    resp = c.post("/register", json={"device_token": "tok"})
    assert resp.status_code == 422


def test_unregister_removes_token(client):
    c, store = client
    c.post(
        "/register",
        json={"device_token": "tok-1", "apns_env": "sandbox", "app_version": "1"},
    )
    assert store.get("tok-1") is not None

    resp = c.post("/unregister", json={"device_token": "tok-1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["removed"] is True
    assert store.get("tok-1") is None


def test_unregister_unknown_token_reports_not_removed(client):
    c, _ = client
    resp = c.post("/unregister", json={"device_token": "ghost"})
    assert resp.status_code == 200
    assert resp.json()["removed"] is False


# ---------------------------------------------------------------------------
# /test — Settings "Send test notification" (C6)
# ---------------------------------------------------------------------------


class _SpySender:
    """Records the payload handed to deliver_now() without touching the network."""

    def __init__(self, results=None):
        self.delivered = []
        self._results = results if results is not None else []

    def deliver_now(self, payload):
        self.delivered.append(payload)
        return self._results


def test_test_route_404_when_pipeline_not_wired(client):
    c, _ = client
    # No sender set (the fixture leaves _sender as None) → capability-gate 404.
    resp = c.post("/test", json={})
    assert resp.status_code == 404


def test_test_route_sends_generic_payload_to_registered_devices(client):
    c, store = client
    spy = _SpySender()
    api.set_sender(spy)
    # Two registered devices.
    c.post("/register", json={"device_token": "a", "apns_env": "sandbox", "app_version": "1"})
    c.post("/register", json={"device_token": "b", "apns_env": "production", "app_version": "1"})

    resp = c.post("/test", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["devices"] == 2

    # One generic payload delivered synchronously (fan-out happens inside
    # GatewaySender). It honors the no-content privacy rule: only type/title/body
    # + a synthetic session id — no real content.
    assert len(spy.delivered) == 1
    payload = spy.delivered[0]
    assert payload["type"] == "complete"
    assert payload["session_id"].startswith("test-")
    assert payload["thread_id"] == payload["session_id"]
    assert payload["title"] and payload["body"]
    # No content leaked: the payload keys are exactly the generic set.
    assert set(payload.keys()) == {"type", "session_id", "title", "body", "thread_id"}


def test_test_route_works_with_no_devices(client):
    c, _ = client
    spy = _SpySender()
    api.set_sender(spy)
    resp = c.post("/test", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["devices"] == 0
    assert body["results"] == []
    # With zero devices we short-circuit — no delivery attempted at all.
    assert spy.delivered == []


# -- Synchronous per-device result reporting (the diagnostic fix) ------------


def test_test_route_reports_delivered_on_gateway_2xx(client):
    c, store = client
    http = _FakeHttp(push=[HttpResponse(status=200, body='{"ok":true}')])
    api.set_sender(_wire_sender(store, http))
    c.post(
        "/register",
        json={"device_token": "tok-abcd23346", "apns_env": "sandbox", "app_version": "1"},
    )

    body = c.post("/test", json={}).json()
    assert body["devices"] == 1
    assert len(body["results"]) == 1
    result = body["results"][0]
    assert result["delivered"] is True
    assert result["status"] == 200
    assert result["error"] is None
    assert result["pruned"] is False
    # Device token is MASKED to the last 6 chars — never the full token.
    assert result["device"] == "…d23346"
    assert "tok-abcd23346" not in json.dumps(body)


def test_test_route_reports_status_on_gateway_4xx(client):
    c, store = client
    http = _FakeHttp(push=[HttpResponse(status=400, body="bad request")])
    api.set_sender(_wire_sender(store, http))
    c.post(
        "/register",
        json={"device_token": "tok-1", "apns_env": "sandbox", "app_version": "1"},
    )

    body = c.post("/test", json={}).json()
    result = body["results"][0]
    assert result["delivered"] is False
    assert result["status"] == 400
    assert result["error"] == "gateway_status_400"


def test_test_route_reports_no_capability_when_register_fails(client):
    c, store = client
    # Register (capability fetch) returns a non-2xx → no capability obtainable.
    http = _FakeHttp(register=[HttpResponse(status=500, body="boom")])
    api.set_sender(_wire_sender(store, http))
    c.post(
        "/register",
        json={"device_token": "tok-1", "apns_env": "sandbox", "app_version": "1"},
    )

    body = c.post("/test", json={}).json()
    result = body["results"][0]
    assert result["delivered"] is False
    assert result["error"] == "no_capability"
    # No push was attempted (capability could not be obtained).
    assert http.push_calls == []
