"""Suppression / dedup policy (Task B4).

This is the *decision* layer that sits between the trigger mapper (B3,
``triggers.py``) and the outbound gateway sender (B5, ``sender.py``). Given a
mapped, generic push payload it answers a single question: *should we actually
push this right now?*

The policy is pure and deterministic given its injected dependencies, so the
whole thing is unit-testable without a running agent. Everything that touches
real agent / OS state — the wall clock, "is a live client bound to this
session?", and "how many devices are registered?" — is injected as a callable.
B5 wires the real implementations.

Gates (evaluated cheap-first so we bail out as early as possible):

1. **No devices fast path.** If there are zero registered devices there is
   nobody to notify, so short-circuit before consulting anything more
   expensive (the live-client check, dedup bookkeeping, etc.).

2. **No live client.** Push only when *no* live client is currently bound to
   the session. If the user is actively viewing the session in the app, the
   in-app WebSocket already delivers the event; a push would just buzz a phone
   that's already showing the content.

3. **Duration gate (complete / error only).** Turn-complete and error pushes
   additionally require the turn to have run longer than a threshold
   (default 10s). A turn that finishes near-instantly didn't keep the user
   waiting, so a "done" buzz is noise. ``approval`` and ``clarify`` always need
   the user's attention and are **never** duration-gated.

4. **Dedup.** Collapse rapid repeats of the same ``(session_id, type)`` within
   a short window (default 5s) — e.g. an error storm. The APNs ``collapse-id``
   reinforces this on the delivery side, but we also avoid even sending them.

The turn-start notion (for the duration gate) is fed in by the caller: the
caller records a turn start (``note_turn_start(session_id)``) on the
turn-start / ``message.start`` signal and clears it on completion; at
complete/error the policy computes elapsed = ``now - turn_start`` using the
injected clock. If no turn start is known the turn is treated as "long enough"
(fail-open — better to over-notify a real completion than swallow it).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple

from hermes_push.triggers import TYPE_COMPLETE, TYPE_ERROR

logger = logging.getLogger(__name__)

# Injected-dependency signatures.
#
# * Clock — monotonic seconds. ``time.monotonic`` by default; tests inject a
#   controllable fake. Monotonic (not wall-clock) so dedup / duration windows
#   are immune to NTP steps.
Clock = Callable[[], float]
# * ClientPresent — "is a live client currently bound to session_id?".
#   Backed in B5 by the gateway/session_store transport state (see module note
#   at the bottom of this file).
ClientPresent = Callable[[str], bool]
# * DeviceCount — number of registered devices (cheap; the no-devices gate).
DeviceCount = Callable[[], int]

# Defaults (seconds). Both configurable via the policy constructor.
DEFAULT_DURATION_THRESHOLD_S = 10.0
DEFAULT_DEDUP_WINDOW_S = 5.0

# The payload types that are duration-gated. Approval / clarify always need the
# user, so they are deliberately excluded.
_DURATION_GATED_TYPES = frozenset({TYPE_COMPLETE, TYPE_ERROR})


@dataclass(frozen=True)
class Decision:
    """The outcome of :meth:`SuppressionPolicy.decide`.

    ``send`` is the bottom line. ``reason`` is a short, content-free tag naming
    the gate that decided it — handy for metrics / debug logging. It never
    contains any payload content.
    """

    send: bool
    reason: str


@dataclass
class _PolicyConfig:
    duration_threshold_s: float = DEFAULT_DURATION_THRESHOLD_S
    dedup_window_s: float = DEFAULT_DEDUP_WINDOW_S


class SuppressionPolicy:
    """Combines the four suppression gates into one ``should_send`` decision.

    All external state is injected:

    * ``client_present(session_id) -> bool`` — gate 2.
    * ``device_count() -> int`` — gate 1 (no-devices fast path).
    * ``clock() -> float`` — monotonic seconds, drives gates 3 & 4.

    The policy keeps a little internal bookkeeping (per-session turn start for
    the duration gate, and last-sent timestamps for dedup), guarded by a lock so
    it is safe to call from the couple of hook / WS threads the plugin uses.
    """

    def __init__(
        self,
        *,
        client_present: ClientPresent,
        device_count: DeviceCount,
        clock: Optional[Clock] = None,
        duration_threshold_s: float = DEFAULT_DURATION_THRESHOLD_S,
        dedup_window_s: float = DEFAULT_DEDUP_WINDOW_S,
    ) -> None:
        self._client_present = client_present
        self._device_count = device_count
        self._clock: Clock = clock or time.monotonic
        self._cfg = _PolicyConfig(
            duration_threshold_s=float(duration_threshold_s),
            dedup_window_s=float(dedup_window_s),
        )
        self._lock = threading.RLock()
        # session_id -> monotonic turn-start instant.
        self._turn_starts: Dict[str, float] = {}
        # (session_id, type) -> monotonic instant of the last *sent* push.
        self._last_sent: Dict[Tuple[str, str], float] = {}

    # -- turn-start bookkeeping (feeds the duration gate) -----------------

    def note_turn_start(self, session_id: str) -> None:
        """Record that a turn started for ``session_id`` (now, per the clock).

        Called by the caller on the turn-start / ``message.start`` signal. The
        duration gate reads this at complete/error to compute elapsed.
        """
        with self._lock:
            self._turn_starts[str(session_id)] = self._clock()

    def clear_turn_start(self, session_id: str) -> None:
        """Forget a session's turn start (call on completion / interrupt).

        Idempotent — clearing an unknown session is a no-op.
        """
        with self._lock:
            self._turn_starts.pop(str(session_id), None)

    # -- the decision ----------------------------------------------------

    def decide(self, payload: Dict[str, str]) -> Decision:
        """Decide whether ``payload`` should be pushed, cheap-first.

        ``payload`` is a generic mapped payload from ``triggers.make_payload``
        (``{type, session_id, ...}``); only ``type`` and ``session_id`` are
        read here. Returns a :class:`Decision`. This call does **not** clear the
        turn start — the caller owns that lifecycle (so a retry can re-decide).
        """
        payload_type = str(payload.get("type") or "")
        session_id = str(payload.get("session_id") or "")

        # Gate 1 — no devices: short-circuit before any other (more expensive)
        # check. Nobody to notify.
        if self._device_count() <= 0:
            return Decision(send=False, reason="no_devices")

        # Gate 2 — a live client is viewing this session: the in-app socket
        # already has the event, so don't double-notify.
        if self._client_present(session_id):
            return Decision(send=False, reason="client_present")

        # Gate 3 — duration gate for complete / error only.
        if payload_type in _DURATION_GATED_TYPES:
            if not self._turn_long_enough(session_id):
                return Decision(send=False, reason="short_turn")

        # Gate 4 — dedup rapid repeats of the same (session, type).
        if self._is_duplicate(payload_type, session_id):
            return Decision(send=False, reason="dedup")

        # Passed every gate — record the send for the dedup window and allow.
        self._record_sent(payload_type, session_id)
        return Decision(send=True, reason="ok")

    def should_send(self, payload: Dict[str, str]) -> bool:
        """Boolean convenience wrapper over :meth:`decide`."""
        return self.decide(payload).send

    # -- gate internals --------------------------------------------------

    def _turn_long_enough(self, session_id: str) -> bool:
        """True if the turn ran longer than the duration threshold.

        Fail-open: an unknown turn start (e.g. the plugin attached mid-turn) is
        treated as long enough so we never swallow a real completion.
        """
        with self._lock:
            start = self._turn_starts.get(session_id)
            if start is None:
                return True
            elapsed = self._clock() - start
        return elapsed > self._cfg.duration_threshold_s

    def _is_duplicate(self, payload_type: str, session_id: str) -> bool:
        """True if the same (session, type) was sent within the dedup window."""
        key = (session_id, payload_type)
        with self._lock:
            last = self._last_sent.get(key)
            if last is None:
                return False
            return (self._clock() - last) < self._cfg.dedup_window_s

    def _record_sent(self, payload_type: str, session_id: str) -> None:
        with self._lock:
            self._last_sent[(session_id, payload_type)] = self._clock()
