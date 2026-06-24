"""Trigger hooks → generic push-payload mapping.

This is the *mapping* layer of the plugin. It turns each trigger event the iOS
app cares about into a generic, privacy-safe payload dict and hands it to an
injectable **sink** (a callback). The suppression policy (``policy.py``) and the
outbound POST to the gateway (``sender.py``) plug in behind that sink.

Privacy rule (hard requirement)
-------------------------------
Only a generic ``{type, session_id, title, body, thread_id}`` ever leaves this
plugin. We **never** copy message content, tool args, reasoning text, the
approval command, the assistant response, or conversation history into the
payload. ``title``/``body`` are fixed constants (see :data:`TITLES` /
:data:`BODIES`) so tests can assert them and a future translator has one place to
look. The real content is fetched in-app over Tailscale once the user taps the
notification.

How the events surface (verified against hermes-agent — see STEP 0 findings)
----------------------------------------------------------------------------
* **approval** — the ``pre_approval_request`` hook (``tools/approval.py``). An
  *observer* hook fired both for CLI prompts and gateway/remote approvals,
  exposing ``surface`` ("cli" | "gateway") and ``session_key`` (== the gateway
  ``session_id``). We push only for gateway/remote surfaces and ignore CLI-only
  requests.

* **complete** — the ``post_llm_call`` hook (``agent/conversation_loop.py``),
  fired once per turn after the tool-loop finishes and a final assistant
  response is produced, in BOTH CLI and gateway. We read ONLY ``session_id``
  from it (never ``user_message`` / ``assistant_response`` / ``conversation_history``).

* **error** — the ``on_session_end`` hook (``agent/conversation_loop.py``),
  fired at the end of every ``run_conversation()`` call. We push only on a
  genuine failure (``completed is False AND interrupted is False``); success is
  already covered by ``post_llm_call`` and interrupts are user-initiated.

* **clarify** — NOT currently supported. The agent's clarify/input-needed event
  is emitted straight to the per-session WebSocket transport and has no
  registerable plugin hook, so the plugin cannot observe it. The ``clarify``
  payload type is kept here for forward-compatibility but nothing maps to it.

This module keeps the *pure* mapping (hook kwargs → payload) separate from any
I/O so the suite can assert payloads without a running agent.
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

# The payload ``type`` values, matching the gateway-fn / iOS contract.
# ``clarify`` is kept for forward-compatibility but is NOT currently produced —
# the agent's clarify event has no registerable plugin hook (see module docstring).
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


def _hook_session_id(kwargs: Dict[str, Any]) -> str:
    """Pull ``session_id`` from a ``post_llm_call`` / ``on_session_end`` hook.

    Both fire with ``session_id=agent.session_id`` (the same key the gateway uses
    for ``pre_approval_request``\\'s ``session_key`` and the WS frame, so push
    threads collapse correctly). We read ONLY the id — never the message content.
    """
    sid = kwargs.get("session_id") or kwargs.get("session_key") or ""
    return str(sid)


# ---------------------------------------------------------------------------
# Mappers (hook kwargs → payload, or None to skip)
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


def map_complete(**kwargs: Any) -> Optional[Dict[str, str]]:
    """Map a ``post_llm_call`` hook invocation → a generic ``complete`` payload.

    Fired once per successful turn (CLI + gateway). Reads ONLY ``session_id`` —
    NEVER ``user_message`` / ``assistant_response`` / ``conversation_history``
    (privacy rule). Returns ``None`` if no session id is available.
    """
    session_id = _hook_session_id(kwargs)
    if not session_id:
        return None
    return make_payload(TYPE_COMPLETE, session_id)


def map_session_end(**kwargs: Any) -> Optional[Dict[str, str]]:
    """Map an ``on_session_end`` hook invocation → an ``error`` payload, or skip.

    ``on_session_end`` fires at the end of EVERY ``run_conversation()`` call —
    success, failure, and interrupt alike. We map to an ``error`` push ONLY on a
    genuine failure (``completed is False AND interrupted is False``):

    * success → ``post_llm_call`` already produced a ``complete`` push, so skip.
    * interrupted → user-initiated stop; never notify.

    Reads only ``completed`` / ``interrupted`` / ``session_id`` — never content.
    """
    completed = bool(kwargs.get("completed"))
    interrupted = bool(kwargs.get("interrupted"))
    if completed or interrupted:
        return None
    session_id = _hook_session_id(kwargs)
    if not session_id:
        return None
    return make_payload(TYPE_ERROR, session_id)


# ---------------------------------------------------------------------------
# Trigger dispatcher — owns the sink, wires the hook callbacks
# ---------------------------------------------------------------------------


class TriggerDispatcher:
    """Owns the push sink and turns surfaced events into payloads.

    Wires the ``pre_approval_request`` / ``post_llm_call`` / ``on_session_end``
    hook callbacks. Each mapped payload is handed to the injectable ``sink``; the
    default no-op makes the dispatcher safe to attach before the pipeline is
    wired. The dispatcher never raises into the host — a sink failure is logged
    and swallowed so it can never disrupt a turn.
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

    # -- turn-complete hook ----------------------------------------------

    def on_post_llm_call(self, **kwargs: Any) -> None:
        """``post_llm_call`` hook callback (observer; returns None).

        Maps a successful turn to a generic ``complete`` payload and feeds the
        sink. Reads only ``session_id`` — never the message content kwargs.
        """
        self._emit_payload(map_complete(**kwargs))
        return None

    # -- session-end (error) hook ----------------------------------------

    def on_session_end(self, **kwargs: Any) -> None:
        """``on_session_end`` hook callback (observer; returns None).

        Maps a genuine turn failure to an ``error`` payload and feeds the sink
        (success / interrupt produce nothing). The plugin separately clears the
        policy's turn-start anchor here (see ``__init__``).
        """
        self._emit_payload(map_session_end(**kwargs))
        return None
