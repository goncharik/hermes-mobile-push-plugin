"""Tests for the B4 suppression / dedup policy.

Each gate is exercised independently with injected clock + dependency callables,
so nothing here needs a running agent. The no-devices fast path is additionally
asserted to short-circuit *before* the more expensive checks via spies.
"""

from __future__ import annotations

from typing import Dict, List

import pytest

from hermes_push.policy import (
    DEFAULT_DEDUP_WINDOW_S,
    DEFAULT_DURATION_THRESHOLD_S,
    Decision,
    SuppressionPolicy,
)
from hermes_push.triggers import (
    TYPE_APPROVAL,
    TYPE_CLARIFY,
    TYPE_COMPLETE,
    TYPE_ERROR,
    make_payload,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeClock:
    """A controllable monotonic clock."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


class Spy:
    """A callable that records how many times it was invoked."""

    def __init__(self, return_value) -> None:
        self.return_value = return_value
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self.return_value


SID = "sess-1"


def _policy(
    *,
    client_present=lambda _sid: False,
    device_count=lambda: 1,
    clock=None,
    duration_threshold_s=DEFAULT_DURATION_THRESHOLD_S,
    dedup_window_s=DEFAULT_DEDUP_WINDOW_S,
) -> SuppressionPolicy:
    return SuppressionPolicy(
        client_present=client_present,
        device_count=device_count,
        clock=clock,
        duration_threshold_s=duration_threshold_s,
        dedup_window_s=dedup_window_s,
    )


# ---------------------------------------------------------------------------
# Gate 2 — live client present / absent
# ---------------------------------------------------------------------------


def test_client_present_suppresses():
    policy = _policy(client_present=lambda _sid: True)
    decision = policy.decide(make_payload(TYPE_APPROVAL, SID))
    assert decision == Decision(send=False, reason="client_present")
    assert policy.should_send(make_payload(TYPE_APPROVAL, SID)) is False


def test_client_absent_allows():
    policy = _policy(client_present=lambda _sid: False)
    assert policy.should_send(make_payload(TYPE_APPROVAL, SID)) is True


def test_client_present_is_keyed_by_session():
    # Only the matching session is considered "present".
    present = {"other-sess"}
    policy = _policy(client_present=lambda sid: sid in present)
    assert policy.should_send(make_payload(TYPE_APPROVAL, SID)) is True
    assert policy.should_send(make_payload(TYPE_APPROVAL, "other-sess")) is False


# ---------------------------------------------------------------------------
# Gate 3 — duration gate (complete only)
# ---------------------------------------------------------------------------


def test_short_turn_suppresses_complete():
    clock = FakeClock()
    policy = _policy(clock=clock)
    policy.note_turn_start(SID)
    clock.advance(DEFAULT_DURATION_THRESHOLD_S)  # exactly threshold -> not > threshold
    decision = policy.decide(make_payload(TYPE_COMPLETE, SID))
    assert decision == Decision(send=False, reason="short_turn")


def test_long_turn_allows_complete():
    clock = FakeClock()
    policy = _policy(clock=clock)
    policy.note_turn_start(SID)
    clock.advance(DEFAULT_DURATION_THRESHOLD_S + 0.5)  # over threshold
    assert policy.should_send(make_payload(TYPE_COMPLETE, SID)) is True


@pytest.mark.parametrize("ptype", [TYPE_APPROVAL, TYPE_CLARIFY, TYPE_ERROR])
def test_approval_clarify_and_error_are_not_duration_gated(ptype):
    # A near-instant turn still pushes for approval / clarify, and a genuine
    # error is worth surfacing even on a short turn.
    clock = FakeClock()
    policy = _policy(clock=clock)
    policy.note_turn_start(SID)
    clock.advance(0.1)  # well under the threshold
    assert policy.should_send(make_payload(ptype, SID)) is True


def test_unknown_turn_start_fails_open_for_complete():
    # No note_turn_start() called -> treated as long enough (don't swallow).
    policy = _policy(clock=FakeClock())
    assert policy.should_send(make_payload(TYPE_COMPLETE, SID)) is True


def test_clear_turn_start_drops_anchor():
    clock = FakeClock()
    policy = _policy(clock=clock)
    policy.note_turn_start(SID)
    policy.clear_turn_start(SID)
    clock.advance(0.1)
    # Anchor gone -> fail-open -> allowed despite the short elapsed.
    assert policy.should_send(make_payload(TYPE_COMPLETE, SID)) is True
    # Clearing an unknown session is a harmless no-op.
    policy.clear_turn_start("never-seen")


def test_custom_duration_threshold():
    clock = FakeClock()
    policy = _policy(clock=clock, duration_threshold_s=30.0)
    policy.note_turn_start(SID)
    clock.advance(20.0)  # under the custom 30s threshold
    assert policy.should_send(make_payload(TYPE_COMPLETE, SID)) is False
    clock.advance(11.0)  # now 31s total -> over
    assert policy.should_send(make_payload(TYPE_COMPLETE, SID)) is True


# ---------------------------------------------------------------------------
# Gate 4 — dedup window
# ---------------------------------------------------------------------------


def test_dedup_drops_rapid_repeat_then_allows_after_window():
    clock = FakeClock()
    policy = _policy(clock=clock)
    p = make_payload(TYPE_ERROR, SID)
    # First one is allowed (and recorded).
    assert policy.decide(p) == Decision(send=True, reason="ok")
    # A repeat within the window is dropped.
    clock.advance(DEFAULT_DEDUP_WINDOW_S - 1.0)
    assert policy.decide(p) == Decision(send=False, reason="dedup")
    # After the window it is allowed again.
    clock.advance(2.0)  # now past the window since the first send
    assert policy.decide(p) == Decision(send=True, reason="ok")


def test_dedup_is_per_session_and_per_type():
    clock = FakeClock()
    policy = _policy(clock=clock)
    assert policy.should_send(make_payload(TYPE_ERROR, SID)) is True
    # Different type, same session -> not a dup.
    assert policy.should_send(make_payload(TYPE_CLARIFY, SID)) is True
    # Different session, same type -> not a dup.
    assert policy.should_send(make_payload(TYPE_ERROR, "sess-2")) is True
    # Same session + type within window -> dup.
    assert policy.should_send(make_payload(TYPE_ERROR, SID)) is False


def test_suppressed_payload_does_not_count_toward_dedup():
    # A payload dropped by an earlier gate must not poison the dedup window so
    # that a later legitimate one is wrongly dropped.
    clock = FakeClock()
    present = {SID}
    policy = _policy(clock=clock, client_present=lambda sid: sid in present)
    # Client present -> suppressed, not recorded.
    assert policy.decide(make_payload(TYPE_ERROR, SID)).reason == "client_present"
    # Client leaves; the same payload should now send (no stale dedup record).
    present.clear()
    assert policy.decide(make_payload(TYPE_ERROR, SID)) == Decision(send=True, reason="ok")


# ---------------------------------------------------------------------------
# Gate 1 — no-devices fast path (and that it short-circuits)
# ---------------------------------------------------------------------------


def test_no_devices_fast_path_suppresses():
    policy = _policy(device_count=lambda: 0)
    decision = policy.decide(make_payload(TYPE_APPROVAL, SID))
    assert decision == Decision(send=False, reason="no_devices")


def test_no_devices_short_circuits_before_expensive_checks():
    # The no-devices gate must run first; the client-present check must NOT be
    # consulted when there are zero devices.
    device_count = Spy(0)
    client_present = Spy(False)
    policy = _policy(device_count=device_count, client_present=client_present)

    policy.decide(make_payload(TYPE_COMPLETE, SID))

    assert device_count.calls == 1
    assert client_present.calls == 0  # never consulted on the fast path


def test_devices_present_consults_client_check():
    # Sanity counterpart: with devices, the client-present check IS consulted.
    device_count = Spy(2)
    client_present = Spy(False)
    policy = _policy(device_count=device_count, client_present=client_present)

    policy.decide(make_payload(TYPE_APPROVAL, SID))

    assert device_count.calls == 1
    assert client_present.calls == 1
