"""Tests for the B3 trigger → generic-payload mapping layer.

Covers all four trigger types (approval, clarify, complete, error), the
approval ``surface`` gating (gateway → mapped, cli → skipped), the loopback WS
event mapper (for complete/error/clarify, which have no hook path), and the
privacy rule: no message content / args / reasoning / command / question may
leak into a payload.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from hermes_push import triggers
from hermes_push.triggers import (
    BODIES,
    TITLES,
    TYPE_APPROVAL,
    TYPE_CLARIFY,
    TYPE_COMPLETE,
    TYPE_ERROR,
    TriggerDispatcher,
    make_payload,
    map_approval,
    map_ws_event,
)


# Sentinels that must NEVER appear anywhere in a produced payload. They stand in
# for the kind of sensitive content the host passes alongside each event.
_SECRET_STRINGS = (
    "rm -rf /home/user/secret",   # approval command
    "delete the production db",   # approval description
    "What is your AWS key?",      # clarify question
    "Traceback: secret stack",    # error message
    "Here is the answer: 42 …",   # completed message text
    "<reasoning> chain </reasoning>",
)


def _assert_generic_payload(payload: Dict[str, str], *, ptype: str, sid: str) -> None:
    """Assert a payload is the exact generic shape and leaks no content."""
    assert payload == {
        "type": ptype,
        "session_id": sid,
        "title": TITLES[ptype],
        "body": BODIES[ptype],
        "thread_id": sid,
    }
    # Privacy: the only dynamic value is the (caller-supplied) session id.
    blob = "".join(str(v) for v in payload.values())
    for secret in _SECRET_STRINGS:
        assert secret not in blob, f"content leaked into payload: {secret!r}"


# ---------------------------------------------------------------------------
# make_payload
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ptype", [TYPE_APPROVAL, TYPE_CLARIFY, TYPE_COMPLETE, TYPE_ERROR]
)
def test_make_payload_is_generic_for_every_type(ptype: str) -> None:
    payload = make_payload(ptype, "sess-123")
    _assert_generic_payload(payload, ptype=ptype, sid="sess-123")
    # thread_id collapses on the session.
    assert payload["thread_id"] == payload["session_id"]


def test_make_payload_rejects_unknown_type() -> None:
    with pytest.raises(ValueError):
        make_payload("bogus", "s1")


def test_distinct_titles_and_bodies_per_type() -> None:
    # The four types must be distinguishable by copy alone.
    assert len(set(TITLES.values())) == 4
    assert len(set(BODIES.values())) == 4


# ---------------------------------------------------------------------------
# approval: pre_approval_request hook mapping + surface gating
# ---------------------------------------------------------------------------


def test_approval_gateway_surface_maps_to_payload() -> None:
    payload = map_approval(
        command="rm -rf /home/user/secret",
        description="delete the production db",
        pattern_key="rm",
        pattern_keys=["rm"],
        session_key="sess-approval",
        surface="gateway",
    )
    assert payload is not None
    _assert_generic_payload(payload, ptype=TYPE_APPROVAL, sid="sess-approval")


def test_approval_cli_surface_is_skipped() -> None:
    payload = map_approval(
        command="rm -rf /home/user/secret",
        session_key="sess-approval",
        surface="cli",
    )
    assert payload is None


def test_approval_missing_surface_is_skipped() -> None:
    # No/unknown surface → don't push (fail safe; never spam on CLI-only).
    assert map_approval(session_key="s1") is None
    assert map_approval(session_key="s1", surface="") is None


def test_approval_session_id_falls_back_to_session_id_kwarg() -> None:
    payload = map_approval(surface="gateway", session_id="from-sid")
    assert payload is not None
    assert payload["session_id"] == "from-sid"


def test_approval_empty_session_id_is_skipped() -> None:
    # An empty session id would make the gateway reject (400) → silent drop. Skip instead.
    assert map_approval(surface="gateway") is None
    assert map_approval(surface="gateway", session_key="") is None
    assert map_approval(surface="gateway", session_id="") is None


# ---------------------------------------------------------------------------
# complete / error / clarify: loopback WS event mapping
# ---------------------------------------------------------------------------


def _event_frame(event_type: str, sid: str, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Build a gateway event frame as tui_gateway/server.py::_emit writes it."""
    params: Dict[str, Any] = {"type": event_type, "session_id": sid}
    if payload is not None:
        params["payload"] = payload
    return {"jsonrpc": "2.0", "method": "event", "params": params}


def test_ws_message_complete_maps_to_complete() -> None:
    frame = _event_frame(
        "message.complete",
        "sess-c",
        # The real complete payload carries the rendered message + reasoning —
        # none of it may leak.
        payload={
            "rendered": "Here is the answer: 42 …",
            "reasoning": "<reasoning> chain </reasoning>",
        },
    )
    payload = map_ws_event(frame)
    assert payload is not None
    _assert_generic_payload(payload, ptype=TYPE_COMPLETE, sid="sess-c")


def test_ws_error_maps_to_error() -> None:
    frame = _event_frame("error", "sess-e", payload={"message": "Traceback: secret stack"})
    payload = map_ws_event(frame)
    assert payload is not None
    _assert_generic_payload(payload, ptype=TYPE_ERROR, sid="sess-e")


def test_ws_clarify_request_maps_to_clarify() -> None:
    frame = _event_frame(
        "clarify.request",
        "sess-q",
        payload={"question": "What is your AWS key?", "choices": ["a", "b"]},
    )
    payload = map_ws_event(frame)
    assert payload is not None
    _assert_generic_payload(payload, ptype=TYPE_CLARIFY, sid="sess-q")


def test_ws_accepts_bare_params_dict() -> None:
    # The mapper tolerates being handed just the params sub-object.
    payload = map_ws_event({"type": "error", "session_id": "s-bare"})
    assert payload is not None
    assert payload["type"] == TYPE_ERROR
    assert payload["session_id"] == "s-bare"


@pytest.mark.parametrize(
    "ignored_type",
    ["message.delta", "message.start", "status.update", "tool.start", "session.info", ""],
)
def test_ws_ignores_non_trigger_events(ignored_type: str) -> None:
    assert map_ws_event(_event_frame(ignored_type, "s1")) is None


def test_ws_ignores_non_dict_frame() -> None:
    assert map_ws_event(None) is None  # type: ignore[arg-type]
    assert map_ws_event("not a frame") is None  # type: ignore[arg-type]


def test_ws_empty_session_id_is_skipped() -> None:
    # Empty session_id → gateway 400 → silent drop. Skip the push instead.
    assert map_ws_event({"type": "message.complete", "session_id": ""}) is None
    assert map_ws_event({"type": "message.complete"}) is None


# ---------------------------------------------------------------------------
# TriggerDispatcher: sink wiring + non-raising behaviour
# ---------------------------------------------------------------------------


def test_dispatcher_feeds_approval_payload_to_sink() -> None:
    collected: List[Dict[str, str]] = []
    d = TriggerDispatcher(sink=collected.append)

    result = d.on_pre_approval_request(surface="gateway", session_key="s1", command="x")
    assert result is None  # observer hook never alters flow
    assert len(collected) == 1
    _assert_generic_payload(collected[0], ptype=TYPE_APPROVAL, sid="s1")


def test_dispatcher_skips_cli_approval_no_sink_call() -> None:
    collected: List[Dict[str, str]] = []
    d = TriggerDispatcher(sink=collected.append)

    d.on_pre_approval_request(surface="cli", session_key="s1")
    assert collected == []


def test_dispatcher_handles_ws_events() -> None:
    collected: List[Dict[str, str]] = []
    d = TriggerDispatcher(sink=collected.append)

    d.handle_ws_event(_event_frame("clarify.request", "sq", payload={"question": "secret?"}))
    d.handle_ws_event(_event_frame("message.delta", "sq"))  # ignored
    d.handle_ws_event(_event_frame("error", "se"))

    assert [p["type"] for p in collected] == [TYPE_CLARIFY, TYPE_ERROR]


def test_dispatcher_default_sink_is_noop_safe() -> None:
    # No sink injected → drops payloads without raising.
    d = TriggerDispatcher()
    d.on_pre_approval_request(surface="gateway", session_key="s1")
    d.handle_ws_event(_event_frame("error", "se"))  # must not raise


def test_dispatcher_swallows_sink_errors() -> None:
    def boom(_payload: Dict[str, str]) -> None:
        raise RuntimeError("sink exploded")

    d = TriggerDispatcher(sink=boom)
    # A failing sink must never propagate into the host hook thread.
    assert d.on_pre_approval_request(surface="gateway", session_key="s1") is None
    d.handle_ws_event(_event_frame("error", "se"))


def test_set_sink_replaces_sink() -> None:
    first: List[Dict[str, str]] = []
    second: List[Dict[str, str]] = []
    d = TriggerDispatcher(sink=first.append)
    d.set_sink(second.append)

    d.on_pre_approval_request(surface="gateway", session_key="s1")
    assert first == []
    assert len(second) == 1


# ---------------------------------------------------------------------------
# register() binds the approval hook to the shared dispatcher
# ---------------------------------------------------------------------------


def test_register_binds_approval_hook_to_dispatcher(monkeypatch) -> None:
    import hermes_push
    from hermes_push.wsclient import LoopbackWsConnector

    # Keep register() hermetic: no real loopback WS reader thread.
    monkeypatch.setattr(LoopbackWsConnector, "start", lambda self: None)

    collected: List[Dict[str, str]] = []
    try:
        class FakeCtx:
            def __init__(self) -> None:
                self.hooks: Dict[str, list] = {}

            def register_hook(self, name: str, cb: Any) -> None:
                self.hooks.setdefault(name, []).append(cb)

        ctx = FakeCtx()
        hermes_push.register(ctx)
        # register() now wires the policy+sender pipeline as the sink; override it
        # with a collector AFTER register so we can observe the mapped payload
        # without standing up the gateway POST.
        hermes_push.dispatcher.set_sink(collected.append)

        approval_cb = ctx.hooks["pre_approval_request"][0]
        assert approval_cb(surface="gateway", session_key="s-reg") is None
        assert len(collected) == 1
        _assert_generic_payload(collected[0], ptype=TYPE_APPROVAL, sid="s-reg")
    finally:
        # Restore the no-op sink so other tests aren't affected.
        hermes_push.dispatcher.set_sink(triggers._noop_sink)
