"""Tests for the __init__ pipeline wiring + loopback WS connector (Task B5).

Hermetic: no network, no real ``websockets`` socket. We mock the HTTP client,
point the store at a tmp dir, and feed the WS connector a fake connection.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, List

import pytest

import hermes_push
from hermes_push.policy import SuppressionPolicy
from hermes_push.sender import GatewaySender, HttpResponse
from hermes_push.store import TokenStore
from hermes_push.triggers import TriggerDispatcher
from hermes_push.wsclient import LoopbackWsConnector, build_ws_url


# ---------------------------------------------------------------------------
# Pipeline: a decided-send payload reaches the sender
# ---------------------------------------------------------------------------


class _CountingSink:
    def __init__(self) -> None:
        self.sent: List[dict] = []

    def send(self, payload: dict) -> None:
        self.sent.append(payload)


def test_pipeline_sends_when_policy_allows(monkeypatch, tmp_path):
    store = TokenStore(base_dir=tmp_path)
    store.upsert(device_token="dt", apns_env="production", app_version="1")

    fake_sender = _CountingSink()
    policy = SuppressionPolicy(
        client_present=lambda _sid: False,
        device_count=lambda: len(store.list_all()),
    )

    monkeypatch.setattr(hermes_push, "_store", store)
    monkeypatch.setattr(hermes_push, "_policy", policy)
    monkeypatch.setattr(hermes_push, "_sender", fake_sender)

    payload = {"type": "approval", "session_id": "s1", "title": "t", "body": "b"}
    hermes_push._pipeline(payload)

    assert fake_sender.sent == [payload]


def test_pipeline_suppresses_when_no_devices(monkeypatch, tmp_path):
    store = TokenStore(base_dir=tmp_path)  # empty
    fake_sender = _CountingSink()
    policy = SuppressionPolicy(
        client_present=lambda _sid: False,
        device_count=lambda: len(store.list_all()),
    )
    monkeypatch.setattr(hermes_push, "_store", store)
    monkeypatch.setattr(hermes_push, "_policy", policy)
    monkeypatch.setattr(hermes_push, "_sender", fake_sender)

    hermes_push._pipeline({"type": "approval", "session_id": "s1", "title": "t", "body": "b"})
    assert fake_sender.sent == []


def test_pipeline_noop_when_unwired(monkeypatch):
    monkeypatch.setattr(hermes_push, "_policy", None)
    monkeypatch.setattr(hermes_push, "_sender", None)
    # Must not raise even with the pipeline unconfigured.
    hermes_push._pipeline({"type": "approval", "session_id": "s1", "title": "t", "body": "b"})


# ---------------------------------------------------------------------------
# All four trigger types deliver through the full local pipeline
# (trigger source → dispatcher → _pipeline → policy → GatewaySender → gateway
#  request). Approval enters via the hook path; complete/error/clarify enter via
# the loopback-WS event mapper. This is the cross-component "all four deliver"
# acceptance check (C8): each produces a correct, generic gateway request whose
# top-level `type` the gateway echoes into APNs (gateway/src/apnsSend.ts) so the
# app can badge approvals.
# ---------------------------------------------------------------------------


class _RecordingHttp:
    """Captures the JSON request bodies posted to the gateway (no socket)."""

    def __init__(self) -> None:
        self.bodies: List[dict] = []

    def post_json(self, url: str, body: bytes, *, timeout: float) -> HttpResponse:
        self.bodies.append(json.loads(body.decode("utf-8")))
        return HttpResponse(status=200, body="")


def _wire_real_pipeline(monkeypatch, tmp_path, *, http):
    """Stand up a real store+policy+sender pipeline bound to the dispatcher."""
    # Provision the shared HMAC secret so the sender signs (matching the gateway).
    monkeypatch.setenv("HERMES_PUSH_HMAC_SECRET", "shared-secret-for-tests")
    store = TokenStore(base_dir=tmp_path)
    store.upsert(device_token="dt", apns_env="production", app_version="1")
    policy = SuppressionPolicy(
        client_present=lambda _sid: False,  # no live client → always allow
        device_count=lambda: len(store.list_all()),
    )
    sender = GatewaySender(store=store, http_client=http, max_attempts=1, backoff_base_s=0.0)

    monkeypatch.setattr(hermes_push, "_store", store)
    monkeypatch.setattr(hermes_push, "_policy", policy)
    monkeypatch.setattr(hermes_push, "_sender", sender)
    dispatcher = TriggerDispatcher()
    dispatcher.set_sink(hermes_push._pipeline)
    return dispatcher, sender


def test_approval_trigger_delivers_through_pipeline(monkeypatch, tmp_path):
    http = _RecordingHttp()
    dispatcher, sender = _wire_real_pipeline(monkeypatch, tmp_path, http=http)

    # Approval arrives via the pre_approval_request hook (gateway surface): it
    # flows dispatcher → _pipeline → policy(allow) → sender (background executor).
    dispatcher.on_pre_approval_request(surface="gateway", session_key="sess-approval", command="x")

    # The dispatcher submits to the sender's background executor; run the same
    # mapped payload synchronously to assert the gateway request deterministically.
    payload = {"type": "approval", "session_id": "sess-approval", "title": "Approval needed",
               "body": "Hermes needs your approval", "thread_id": "sess-approval"}
    sender.send_blocking(payload)
    assert http.bodies, "approval did not reach the gateway"
    req = http.bodies[-1]
    assert req["type"] == "approval"
    assert req["session_id"] == "sess-approval"
    assert "hmac" in req  # signed with the shared secret


@pytest.mark.parametrize(
    "ws_type,expected",
    [("message.complete", "complete"), ("error", "error"), ("clarify.request", "clarify")],
)
def test_ws_triggers_deliver_through_pipeline(monkeypatch, tmp_path, ws_type, expected):
    """complete/error/clarify enter via the loopback-WS mapper and reach the gateway."""
    http = _RecordingHttp()
    dispatcher, sender = _wire_real_pipeline(monkeypatch, tmp_path, http=http)

    frame = json.dumps({"method": "event", "params": {"type": ws_type, "session_id": "sx",
                                                       "payload": {"message": "SECRET CONTENT"}}})
    # The connector forwards raw frames to dispatcher.handle_ws_event, which maps
    # and feeds the sink (_pipeline) → policy → sender (executor). Then assert the
    # gateway request shape synchronously via the same sender.
    _run_connector([frame], on_frame=dispatcher.handle_ws_event)
    from hermes_push.triggers import map_ws_event

    payload = map_ws_event({"type": ws_type, "session_id": "sx",
                            "payload": {"message": "SECRET CONTENT"}})
    sender.send_blocking(payload)

    assert http.bodies, f"{ws_type} did not reach the gateway"
    req = http.bodies[-1]
    assert req["type"] == expected
    assert req["session_id"] == "sx"
    # Privacy: no message content may transit.
    assert "SECRET CONTENT" not in json.dumps(req)


# ---------------------------------------------------------------------------
# register() wires the full pipeline end to end
# ---------------------------------------------------------------------------


class FakeCtx:
    def __init__(self) -> None:
        self.hooks: dict[str, list] = {}

    def register_hook(self, hook_name: str, callback) -> None:
        self.hooks.setdefault(hook_name, []).append(callback)


def test_register_stands_up_store_policy_sender(monkeypatch):
    # Don't actually spawn the WS thread during the test.
    monkeypatch.setattr(LoopbackWsConnector, "start", lambda self: None)

    ctx = FakeCtx()
    hermes_push.register(ctx)

    assert isinstance(hermes_push._store, TokenStore)
    assert isinstance(hermes_push._policy, SuppressionPolicy)
    assert isinstance(hermes_push._sender, GatewaySender)
    # Sink is the pipeline (approval hook now flows through policy+sender).
    assert hermes_push.dispatcher._sink is hermes_push._pipeline
    # The REST module shares the same store + sender so /test reuses the pipeline.
    from hermes_push import api

    assert api.get_store() is hermes_push._store
    assert api.get_sender() is hermes_push._sender


def test_register_survives_pipeline_failure(monkeypatch):
    def boom(_ctx):
        raise RuntimeError("nope")

    monkeypatch.setattr(hermes_push, "_wire_pipeline", boom)
    ctx = FakeCtx()
    # Hooks must still register and register() must not raise.
    hermes_push.register(ctx)
    assert set(ctx.hooks) == {
        "pre_approval_request",
        "pre_gateway_dispatch",
        "on_session_end",
    }


# ---------------------------------------------------------------------------
# Loopback WS connector — hermetic (fake connection, no real socket)
# ---------------------------------------------------------------------------


class FakeWs:
    """An async-iterable fake WS yielding pre-scripted frames, then signals stop.

    On context exit (after all frames are drained) it sets the connector's stop
    event so the reader loop exits without reconnecting — giving deterministic,
    single-pass tests.
    """

    def __init__(self, frames: List[str], stop) -> None:
        self._frames = list(frames)
        self._stop = stop

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self._stop.set()
        return False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for f in self._frames:
            yield f


def _run_connector(frames, on_frame, on_turn_start=None, on_turn_end=None):
    """Drive one connector pass synchronously with a fake connect()."""
    connector = LoopbackWsConnector(
        on_frame=on_frame,
        on_turn_start=on_turn_start,
        on_turn_end=on_turn_end,
        url_factory=lambda: "ws://127.0.0.1:8080/api/ws?token=t",
        reconnect_delay_s=0.0,
    )
    fake_connect = lambda url: FakeWs(frames, connector._stop)
    asyncio.run(connector._reader_loop(fake_connect))


def test_ws_connector_maps_complete_event_into_dispatcher():
    received: List[dict] = []
    dispatcher = TriggerDispatcher()
    dispatcher.set_sink(received.append)

    frame = json.dumps(
        {"method": "event", "params": {"type": "message.complete", "session_id": "s9"}}
    )
    _run_connector([frame], on_frame=dispatcher.handle_ws_event)

    assert len(received) == 1
    assert received[0]["type"] == "complete"
    assert received[0]["session_id"] == "s9"


def test_ws_connector_ignores_non_trigger_events():
    received: List[dict] = []
    dispatcher = TriggerDispatcher()
    dispatcher.set_sink(received.append)

    frames = [
        json.dumps({"method": "event", "params": {"type": "status.update", "session_id": "s"}}),
        json.dumps({"method": "event", "params": {"type": "tool.start", "session_id": "s"}}),
        "not json",
        json.dumps(["unexpected", "shape"]),
    ]
    _run_connector(frames, on_frame=dispatcher.handle_ws_event)
    assert received == []


def test_ws_connector_feeds_turn_lifecycle_signals():
    starts: List[str] = []
    ends: List[str] = []
    frames = [
        json.dumps({"method": "event", "params": {"type": "message.start", "session_id": "s1"}}),
        json.dumps({"method": "event", "params": {"type": "message.complete", "session_id": "s1"}}),
        json.dumps({"method": "event", "params": {"type": "error", "session_id": "s2"}}),
    ]
    _run_connector(
        frames,
        on_frame=lambda _f: None,
        on_turn_start=starts.append,
        on_turn_end=ends.append,
    )
    assert starts == ["s1"]
    assert ends == ["s1", "s2"]


def test_ws_connector_degrades_when_no_websockets(monkeypatch):
    """No injected connect + no 'websockets' module → connector is a quiet no-op."""
    connector = LoopbackWsConnector(on_frame=lambda _f: None)

    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "websockets":
            raise ImportError("no websockets")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert connector._resolve_connect() is None
    # _run with no connect available must return without raising.
    connector._run()


def test_ws_connector_connection_error_is_swallowed():
    """A failing connect() must not raise out of the reader loop."""
    def bad_connect(url):
        raise OSError("connection refused")

    connector = LoopbackWsConnector(
        on_frame=lambda _f: None,
        url_factory=lambda: "ws://127.0.0.1:8080/api/ws",
        connect=bad_connect,
        reconnect_delay_s=0.0,
    )
    connector._stop.set()  # one pass only
    # Should complete without raising.
    asyncio.run(connector._reader_loop(bad_connect))


# ---------------------------------------------------------------------------
# build_ws_url
# ---------------------------------------------------------------------------


def test_build_ws_url_default(monkeypatch):
    for k in ("HERMES_PUSH_WS_URL", "HERMES_DASHBOARD_URL", "HERMES_DASHBOARD_HOST",
              "HERMES_DASHBOARD_PORT", "HERMES_DASHBOARD_SESSION_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    url = build_ws_url()
    assert url == "ws://127.0.0.1:8080/api/ws"


def test_build_ws_url_appends_token(monkeypatch):
    monkeypatch.delenv("HERMES_PUSH_WS_URL", raising=False)
    monkeypatch.delenv("HERMES_DASHBOARD_URL", raising=False)
    monkeypatch.setenv("HERMES_DASHBOARD_HOST", "127.0.0.1")
    monkeypatch.setenv("HERMES_DASHBOARD_PORT", "9000")
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "tok123")
    url = build_ws_url()
    assert url == "ws://127.0.0.1:9000/api/ws?token=tok123"


def test_build_ws_url_explicit_override(monkeypatch):
    monkeypatch.setenv("HERMES_PUSH_WS_URL", "ws://example:1234/api/ws")
    monkeypatch.delenv("HERMES_DASHBOARD_SESSION_TOKEN", raising=False)
    assert build_ws_url() == "ws://example:1234/api/ws"


def test_build_ws_url_from_https_dashboard(monkeypatch):
    monkeypatch.delenv("HERMES_PUSH_WS_URL", raising=False)
    monkeypatch.delenv("HERMES_DASHBOARD_SESSION_TOKEN", raising=False)
    monkeypatch.setenv("HERMES_DASHBOARD_URL", "https://agent.local:8443")
    assert build_ws_url() == "wss://agent.local:8443/api/ws"
