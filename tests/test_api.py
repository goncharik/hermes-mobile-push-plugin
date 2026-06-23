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

from hermes_push import api
from hermes_push.store import TokenStore


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
    """Records payloads handed to send() without touching the network."""

    def __init__(self):
        self.sent = []

    def send(self, payload):
        self.sent.append(payload)


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

    # One generic payload handed to the sender (fan-out to devices happens inside
    # GatewaySender). It honors the no-content privacy rule: only type/title/body
    # + a synthetic session id — no real content.
    assert len(spy.sent) == 1
    payload = spy.sent[0]
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
    assert resp.json()["devices"] == 0
    # Still dispatched to the sender (which no-ops over an empty device list).
    assert len(spy.sent) == 1
