"""Trigger hooks → generic push-payload mapping (Task B3).

This is the *mapping* layer of the plugin. It turns each of the four trigger
events the iOS app cares about into a generic, privacy-safe payload dict and
hands it to an injectable **sink** (a callback). Suppression/dedup (B4) and the
outbound POST to the gateway (B5) plug in behind that sink later; for B3 the
sink is a no-op/collector.

Privacy rule (hard requirement)
-------------------------------
Only a generic ``{type, session_id, title, body, thread_id}`` ever leaves this
plugin. We **never** copy message content, tool args, reasoning text, the
approval command, or the clarify question into the payload. ``title``/``body``
are fixed constants (see :data:`TITLES` / :data:`BODIES`) so tests can assert
them and a future translator has one place to look. The real content is fetched
in-app over Tailscale once the user taps the notification.

How the four events surface (verified against hermes-agent — see
docs/plans/2026-06-20-push-notifications.md and the B3 progress note)
--------------------------------------------------------------------
* **approval** — the ``pre_approval_request`` hook
  (``tools/approval.py``). It is an *observer* hook fired both for CLI prompts
  and for gateway/remote approvals, exposing ``surface`` ("cli" | "gateway")
  and ``session_key`` (== the gateway ``session_id``). We push only for
  gateway/remote surfaces and ignore CLI-only requests.

* **complete / error / clarify** — these are emitted by the gateway via
  ``_emit(...)`` (``tui_gateway/server.py``) straight to the WebSocket
  transport; they do **NOT** pass through any registerable plugin hook
  (``pre_gateway_dispatch`` only sees *incoming* user ``MessageEvent``\\s, never
  outbound events). So there is no hook path for them. We therefore use a thin,
  read-only loopback WebSocket client to the agent's local ``/api/ws`` sidecar
  that watches the event stream and maps the relevant event frames. That client
  lives in :func:`map_ws_event` (the pure mapper, fully unit-tested) plus an
  optional, guarded connector that is OFF the hook thread and degrades to a
  no-op if the WS isn't reachable. The exact event ``type`` strings are
  ``"message.complete"``, ``"error"`` and ``"clarify.request"``.

This module keeps the *pure* mapping (hook kwargs / event frame → payload)
separate from any I/O so the suite can assert payloads without a running agent.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# A push payload sink: receives one mapped payload, returns nothing. B4 (policy)
# and B5 (sender) compose behind this. For B3 the default is a no-op.
PushSink = Callable[[Dict[str, str]], None]


def _noop_sink(_payload: Dict[str, str]) -> None:
    """Default sink — drops payloads. Replaced by the policy/sender in B4/B5."""
    return None


# ---------------------------------------------------------------------------
# Payload type tags + generic (content-free) copy
# ---------------------------------------------------------------------------

# The four payload ``type`` values, matching the gateway-fn / iOS contract.
TYPE_APPROVAL = "approval"
TYPE_CLARIFY = "clarify"
TYPE_COMPLETE = "complete"
TYPE_ERROR = "error"

# Generic, content-free copy. Centralized so tests assert exact strings and a
# future localizer has a single source of truth. NEVER interpolate event data
# into these (privacy rule).
TITLES: Dict[str, str] = {
    TYPE_APPROVAL: "Approval needed",
    TYPE_CLARIFY: "Input needed",
    TYPE_COMPLETE: "Turn complete",
    TYPE_ERROR: "Hermes hit an error",
}

BODIES: Dict[str, str] = {
    TYPE_APPROVAL: "Hermes needs your approval",
    TYPE_CLARIFY: "Hermes needs more information",
    TYPE_COMPLETE: "Hermes finished the turn",
    TYPE_ERROR: "Hermes ran into a problem",
}

# Gateway event ``type`` strings (tui_gateway/server.py::_emit) → our payload
# type tag. Only these three outbound events map; everything else is ignored.
WS_EVENT_TYPE_MAP: Dict[str, str] = {
    "message.complete": TYPE_COMPLETE,
    "error": TYPE_ERROR,
    "clarify.request": TYPE_CLARIFY,
}

# The approval surfaces we push for. CLI-interactive approvals are answered at
# the terminal, so they never need a phone notification.
_REMOTE_APPROVAL_SURFACES = frozenset({"gateway"})


# ---------------------------------------------------------------------------
# Pure payload builder
# ---------------------------------------------------------------------------


def make_payload(payload_type: str, session_id: str) -> Dict[str, str]:
    """Build a generic, content-free push payload.

    ``thread_id`` mirrors ``session_id`` so APNs can collapse a session's
    notifications. Raises ``ValueError`` on an unknown type — callers map to a
    known tag first.
    """
    if payload_type not in TITLES:
        raise ValueError(f"unknown push payload type: {payload_type!r}")
    return {
        "type": payload_type,
        "session_id": session_id,
        "title": TITLES[payload_type],
        "body": BODIES[payload_type],
        # session_id doubles as the APNs collapse / thread id.
        "thread_id": session_id,
    }


# ---------------------------------------------------------------------------
# session_id extraction helpers
# ---------------------------------------------------------------------------


def _approval_session_id(kwargs: Dict[str, Any]) -> str:
    """Pull the session id out of ``pre_approval_request`` kwargs.

    The gateway registers approvals under ``session_key`` which equals the
    gateway ``session_id`` (tui_gateway/server.py register_gateway_notify is
    called with the same key it ``_emit``\\s ``session_id`` for). We prefer
    ``session_key`` and fall back to an explicit ``session_id`` if a future
    host passes one.
    """
    sid = kwargs.get("session_key") or kwargs.get("session_id") or ""
    return str(sid)


def _event_session_id(frame: Dict[str, Any]) -> str:
    """Pull ``session_id`` from a gateway event frame.

    ``_emit`` puts it on ``params`` (``{"method":"event","params":{"type":...,
    "session_id":...}}``). We accept either a full frame or a bare ``params``
    dict, and never read the ``payload`` (content) sub-object.
    """
    params = frame.get("params") if isinstance(frame.get("params"), dict) else frame
    sid = params.get("session_id") or ""
    return str(sid)


def _event_type(frame: Dict[str, Any]) -> str:
    params = frame.get("params") if isinstance(frame.get("params"), dict) else frame
    return str(params.get("type") or "")


# ---------------------------------------------------------------------------
# Mappers (hook kwargs / event frame → payload, or None to skip)
# ---------------------------------------------------------------------------


def map_approval(**kwargs: Any) -> Optional[Dict[str, str]]:
    """Map a ``pre_approval_request`` hook invocation → payload, or skip.

    Skips CLI-only approvals (``surface != "gateway"``) — those are handled at
    the terminal and need no push. Returns ``None`` when skipped.
    """
    surface = str(kwargs.get("surface") or "")
    if surface not in _REMOTE_APPROVAL_SURFACES:
        return None
    session_id = _approval_session_id(kwargs)
    if not session_id:
        # The gateway rejects an empty session_id (400) and the sender then gives up
        # silently. Skip the push entirely rather than emit a payload that will be dropped.
        return None
    return make_payload(TYPE_APPROVAL, session_id)


def map_ws_event(frame: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Map a gateway ``/api/ws`` event frame → payload, or ``None`` to ignore.

    Only ``message.complete`` / ``error`` / ``clarify.request`` map; every other
    event type (deltas, status, tool, etc.) is ignored. Reads only ``type`` and
    ``session_id`` from the frame — never the content ``payload``.
    """
    if not isinstance(frame, dict):
        return None
    payload_type = WS_EVENT_TYPE_MAP.get(_event_type(frame))
    if payload_type is None:
        return None
    session_id = _event_session_id(frame)
    if not session_id:
        # Empty session_id → gateway 400 → silent drop. Skip instead.
        return None
    return make_payload(payload_type, session_id)


# ---------------------------------------------------------------------------
# Trigger dispatcher — owns the sink, wires the hook callbacks
# ---------------------------------------------------------------------------


class TriggerDispatcher:
    """Owns the push sink and turns surfaced events into payloads.

    B3 wires the ``pre_approval_request`` hook callback and exposes
    :meth:`handle_ws_event` for the loopback WS client. Each mapped payload is
    handed to the injectable ``sink``; the default no-op makes the dispatcher
    safe to attach before B4/B5 exist. The dispatcher never raises into the
    host — a sink failure is logged and swallowed so it can never disrupt a
    turn.
    """

    def __init__(self, sink: Optional[PushSink] = None) -> None:
        self._sink: PushSink = sink or _noop_sink

    def set_sink(self, sink: PushSink) -> None:
        """Replace the payload sink (used by B4/B5 wiring and by tests)."""
        self._sink = sink

    def _emit_payload(self, payload: Optional[Dict[str, str]]) -> None:
        if payload is None:
            return
        try:
            self._sink(payload)
        except Exception as exc:  # never let a sink error reach the host
            logger.warning("hermes-push: push sink failed: %s", exc)

    # -- approval hook ----------------------------------------------------

    def on_pre_approval_request(self, **kwargs: Any) -> None:
        """``pre_approval_request`` hook callback (observer; returns None).

        Maps gateway-surface approvals to a payload and feeds the sink. CLI
        approvals are skipped. Always returns ``None`` so it can never alter the
        host's approval flow.
        """
        self._emit_payload(map_approval(**kwargs))
        return None

    # -- loopback WS events ----------------------------------------------

    def handle_ws_event(self, frame: Dict[str, Any]) -> None:
        """Feed one ``/api/ws`` event frame through the mapper → sink.

        Called by the (optional, guarded) loopback client for each event it
        reads. complete / error / clarify map; everything else is ignored.
        """
        self._emit_payload(map_ws_event(frame))
