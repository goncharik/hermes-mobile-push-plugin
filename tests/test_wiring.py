"""Tests for the __init__ pipeline wiring.

Hermetic: no network. We mock the HTTP client and point the store at a tmp dir.
"""

from __future__ import annotations

import json
from typing import List

import hermes_push
from hermes_push.policy import SuppressionPolicy
from hermes_push.sender import GatewaySender, HttpResponse
from hermes_push.store import TokenStore
from hermes_push.triggers import TriggerDispatcher


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
# The trigger types deliver through the full local pipeline
# (hook → dispatcher → _pipeline → policy → GatewaySender → gateway request).
# Approval enters via pre_approval_request, complete via post_llm_call, and
# error via on_session_end. Each produces a correct, generic gateway request
# whose top-level `type` the gateway echoes into APNs (gateway/src/apnsSend.ts).
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


def test_complete_trigger_delivers_through_pipeline(monkeypatch, tmp_path):
    """post_llm_call → complete reaches the gateway, leaking no content."""
    http = _RecordingHttp()
    dispatcher, sender = _wire_real_pipeline(monkeypatch, tmp_path, http=http)

    # post_llm_call carries user_message / assistant_response — none may leak.
    dispatcher.on_post_llm_call(
        session_id="sx",
        user_message="SECRET CONTENT",
        assistant_response="SECRET CONTENT",
    )
    from hermes_push.triggers import map_complete

    payload = map_complete(session_id="sx", assistant_response="SECRET CONTENT")
    sender.send_blocking(payload)

    assert http.bodies, "complete did not reach the gateway"
    req = http.bodies[-1]
    assert req["type"] == "complete"
    assert req["session_id"] == "sx"
    assert "SECRET CONTENT" not in json.dumps(req)


def test_clarify_trigger_delivers_through_pipeline(monkeypatch, tmp_path):
    """pre_tool_call(clarify) → clarify reaches the gateway, leaking no args.

    Even a near-instant turn must notify (clarify is NOT duration-gated).
    """
    http = _RecordingHttp()
    dispatcher, sender = _wire_real_pipeline(monkeypatch, tmp_path, http=http)

    # The clarify tool's args carry the question/choices — none may leak.
    dispatcher.on_pre_tool_call(
        tool_name="clarify",
        args={"question": "SECRET CONTENT", "choices": ["SECRET CONTENT"]},
        session_id="sx",
    )
    from hermes_push.triggers import map_clarify

    payload = map_clarify(tool_name="clarify", session_id="sx")
    sender.send_blocking(payload)

    assert http.bodies, "clarify did not reach the gateway"
    req = http.bodies[-1]
    assert req["type"] == "clarify"
    assert req["session_id"] == "sx"
    assert "SECRET CONTENT" not in json.dumps(req)


def test_clarify_non_clarify_tool_does_not_deliver(monkeypatch, tmp_path):
    """A non-clarify tool call must produce no gateway request."""
    http = _RecordingHttp()
    dispatcher, _sender = _wire_real_pipeline(monkeypatch, tmp_path, http=http)

    assert dispatcher.on_pre_tool_call(
        tool_name="shell", args={"command": "x"}, session_id="sx"
    ) is None
    assert http.bodies == []


def test_error_trigger_delivers_through_pipeline(monkeypatch, tmp_path):
    """on_session_end failure → error reaches the gateway."""
    http = _RecordingHttp()
    dispatcher, sender = _wire_real_pipeline(monkeypatch, tmp_path, http=http)

    dispatcher.on_session_end(session_id="sx", completed=False, interrupted=False)
    from hermes_push.triggers import map_session_end

    payload = map_session_end(session_id="sx", completed=False, interrupted=False)
    sender.send_blocking(payload)

    assert http.bodies, "error did not reach the gateway"
    req = http.bodies[-1]
    assert req["type"] == "error"
    assert req["session_id"] == "sx"


# ---------------------------------------------------------------------------
# register() wires the full pipeline end to end
# ---------------------------------------------------------------------------


class FakeCtx:
    def __init__(self) -> None:
        self.hooks: dict[str, list] = {}

    def register_hook(self, hook_name: str, callback) -> None:
        self.hooks.setdefault(hook_name, []).append(callback)


def test_register_stands_up_store_policy_sender(monkeypatch):
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
        "pre_tool_call",
        "pre_llm_call",
        "post_llm_call",
        "on_session_end",
    }


# ---------------------------------------------------------------------------
# Turn-start anchor + duration gate (pre_llm_call → note, complete gate)
# ---------------------------------------------------------------------------


def test_pre_llm_call_records_turn_start_then_gate(monkeypatch, tmp_path):
    """pre_llm_call notes the anchor; a short turn's complete push is gated."""
    http = _RecordingHttp()
    monkeypatch.setenv("HERMES_PUSH_HMAC_SECRET", "shared-secret-for-tests")
    store = TokenStore(base_dir=tmp_path)
    store.upsert(device_token="dt", apns_env="production", app_version="1")

    fake_now = {"t": 0.0}
    policy = SuppressionPolicy(
        client_present=lambda _sid: False,
        device_count=lambda: len(store.list_all()),
        clock=lambda: fake_now["t"],
    )
    sender = GatewaySender(store=store, http_client=http, max_attempts=1, backoff_base_s=0.0)
    monkeypatch.setattr(hermes_push, "_store", store)
    monkeypatch.setattr(hermes_push, "_policy", policy)
    monkeypatch.setattr(hermes_push, "_sender", sender)

    # Turn starts at t=0.
    hermes_push._on_pre_llm_call(session_id="sd")
    # Short turn (2s) — complete is duration-gated and suppressed.
    fake_now["t"] = 2.0
    assert policy.decide(
        {"type": "complete", "session_id": "sd"}
    ).reason == "short_turn"

    # Long turn (15s) — complete passes the gate.
    hermes_push._on_pre_llm_call(session_id="sd")
    fake_now["t"] = 17.0
    assert policy.decide({"type": "complete", "session_id": "sd"}).send is True


def test_on_session_end_clears_turn_start(monkeypatch, tmp_path):
    store = TokenStore(base_dir=tmp_path)
    policy = SuppressionPolicy(
        client_present=lambda _sid: False,
        device_count=lambda: 1,
    )
    monkeypatch.setattr(hermes_push, "_store", store)
    monkeypatch.setattr(hermes_push, "_policy", policy)
    monkeypatch.setattr(hermes_push, "_sender", _CountingSink())

    policy.note_turn_start("sd")
    assert "sd" in policy._turn_starts
    hermes_push._on_session_end(session_id="sd", completed=True, interrupted=False)
    assert "sd" not in policy._turn_starts
