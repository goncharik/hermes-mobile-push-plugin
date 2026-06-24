"""Tests for the trigger → generic-payload mapping layer.

Covers the payload types (approval, clarify, complete, error), the approval
``surface`` gating (gateway → mapped, cli → skipped), the ``post_llm_call`` →
complete and ``on_session_end`` → error hook mappers, and the privacy rule: no
message content / args / reasoning / command / assistant response may leak into a
payload.
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
    map_clarify,
    map_complete,
    map_session_end,
)


# Sentinels that must NEVER appear anywhere in a produced payload. They stand in
# for the kind of sensitive content the host passes alongside each event.
_SECRET_STRINGS = (
    "rm -rf /home/user/secret",   # approval command
    "delete the production db",   # approval description
    "What is your AWS key?",      # user_message
    "Traceback: secret stack",    # error / failure detail
    "Here is the answer: 42 …",   # assistant_response text
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
# complete: post_llm_call hook mapping (reads only session_id, never content)
# ---------------------------------------------------------------------------


def test_post_llm_call_maps_to_complete() -> None:
    # post_llm_call fires with user_message / assistant_response / history; NONE
    # of it may leak into the generic payload.
    payload = map_complete(
        session_id="sess-c",
        task_id="t1",
        turn_id="turn-1",
        user_message="What is your AWS key?",
        assistant_response="Here is the answer: 42 …",
        conversation_history=[{"role": "assistant", "reasoning": "<reasoning> chain </reasoning>"}],
        model="gpt-4",
        platform="gateway",
    )
    assert payload is not None
    _assert_generic_payload(payload, ptype=TYPE_COMPLETE, sid="sess-c")


def test_post_llm_call_session_key_fallback() -> None:
    payload = map_complete(session_key="sk")
    assert payload is not None
    assert payload["session_id"] == "sk"


def test_post_llm_call_empty_session_id_is_skipped() -> None:
    assert map_complete() is None
    assert map_complete(session_id="") is None


# ---------------------------------------------------------------------------
# clarify: pre_tool_call hook mapping (filtered to the clarify tool)
# ---------------------------------------------------------------------------


def test_pre_tool_call_clarify_maps_to_clarify() -> None:
    # The clarify tool's args carry the question/choices — NONE may leak.
    payload = map_clarify(
        tool_name="clarify",
        args={
            "question": "What is your AWS key?",
            "choices": ["delete the production db", "Traceback: secret stack"],
        },
        session_id="sess-clarify",
        task_id="t1",
        turn_id="turn-1",
        tool_call_id="tc-1",
    )
    assert payload is not None
    _assert_generic_payload(payload, ptype=TYPE_CLARIFY, sid="sess-clarify")


def test_pre_tool_call_non_clarify_tool_is_skipped() -> None:
    # pre_tool_call fires for EVERY tool; only clarify should produce a push.
    assert map_clarify(tool_name="shell", args={"command": "rm -rf /home/user/secret"},
                       session_id="s1") is None
    assert map_clarify(tool_name="", session_id="s1") is None
    assert map_clarify(session_id="s1") is None  # no tool_name at all


def test_pre_tool_call_clarify_session_key_fallback() -> None:
    payload = map_clarify(tool_name="clarify", session_key="sk")
    assert payload is not None
    assert payload["session_id"] == "sk"


def test_pre_tool_call_clarify_empty_session_id_is_skipped() -> None:
    assert map_clarify(tool_name="clarify") is None
    assert map_clarify(tool_name="clarify", session_id="") is None


# ---------------------------------------------------------------------------
# error: on_session_end hook mapping (only genuine failures push)
# ---------------------------------------------------------------------------


def test_session_end_failure_maps_to_error() -> None:
    payload = map_session_end(
        session_id="sess-e",
        completed=False,
        interrupted=False,
        model="gpt-4",
        platform="gateway",
    )
    assert payload is not None
    _assert_generic_payload(payload, ptype=TYPE_ERROR, sid="sess-e")


def test_session_end_success_produces_no_error() -> None:
    # Success is already covered by post_llm_call's complete push.
    assert map_session_end(session_id="s", completed=True, interrupted=False) is None


def test_session_end_interrupted_produces_no_error() -> None:
    # User-initiated stop — never notify, even if not "completed".
    assert map_session_end(session_id="s", completed=False, interrupted=True) is None
    assert map_session_end(session_id="s", completed=True, interrupted=True) is None


def test_session_end_failure_empty_session_id_is_skipped() -> None:
    assert map_session_end(completed=False, interrupted=False) is None
    assert map_session_end(session_id="", completed=False, interrupted=False) is None


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


def test_dispatcher_feeds_complete_payload_to_sink() -> None:
    collected: List[Dict[str, str]] = []
    d = TriggerDispatcher(sink=collected.append)

    result = d.on_post_llm_call(
        session_id="s1",
        user_message="What is your AWS key?",
        assistant_response="Here is the answer: 42 …",
    )
    assert result is None
    assert len(collected) == 1
    _assert_generic_payload(collected[0], ptype=TYPE_COMPLETE, sid="s1")


def test_dispatcher_feeds_clarify_payload_to_sink() -> None:
    collected: List[Dict[str, str]] = []
    d = TriggerDispatcher(sink=collected.append)

    result = d.on_pre_tool_call(
        tool_name="clarify",
        args={"question": "What is your AWS key?"},
        session_id="s1",
    )
    assert result is None  # observer hook never blocks the tool
    assert len(collected) == 1
    _assert_generic_payload(collected[0], ptype=TYPE_CLARIFY, sid="s1")


def test_dispatcher_skips_non_clarify_tool_no_sink_call() -> None:
    collected: List[Dict[str, str]] = []
    d = TriggerDispatcher(sink=collected.append)

    assert d.on_pre_tool_call(tool_name="shell", args={"command": "x"}, session_id="s1") is None
    assert collected == []


def test_dispatcher_feeds_error_only_on_failure() -> None:
    collected: List[Dict[str, str]] = []
    d = TriggerDispatcher(sink=collected.append)

    assert d.on_session_end(session_id="ok", completed=True, interrupted=False) is None
    assert d.on_session_end(session_id="int", completed=False, interrupted=True) is None
    assert d.on_session_end(session_id="fail", completed=False, interrupted=False) is None

    assert [p["type"] for p in collected] == [TYPE_ERROR]
    assert collected[0]["session_id"] == "fail"


def test_dispatcher_default_sink_is_noop_safe() -> None:
    # No sink injected → drops payloads without raising.
    d = TriggerDispatcher()
    d.on_pre_approval_request(surface="gateway", session_key="s1")
    d.on_pre_tool_call(tool_name="clarify", session_id="s1")  # must not raise
    d.on_post_llm_call(session_id="s1")  # must not raise
    d.on_session_end(session_id="s1", completed=False, interrupted=False)


def test_dispatcher_swallows_sink_errors() -> None:
    def boom(_payload: Dict[str, str]) -> None:
        raise RuntimeError("sink exploded")

    d = TriggerDispatcher(sink=boom)
    # A failing sink must never propagate into the host hook thread.
    assert d.on_pre_approval_request(surface="gateway", session_key="s1") is None
    assert d.on_pre_tool_call(tool_name="clarify", session_id="s1") is None
    assert d.on_post_llm_call(session_id="s1") is None
    assert d.on_session_end(session_id="s1", completed=False, interrupted=False) is None


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
