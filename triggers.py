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
  exposing ``surface`` ("cli" | "gateway") and ``session_key``. NOTE:
  ``session_key`` is a *source-derived* key that diverges from the gateway
  ``session_id`` (== ``agent.session_id``) the iOS app opens chats by — the hook
  carries NO ``session_id`` of its own. So we correlate the approval to the
  current turn's real ``session_id`` recorded earlier in the same turn (via
  ``pre_llm_call`` / ``pre_tool_call``; see :func:`current_turn_session`), and
  only fall back to ``session_id`` / ``session_key`` if that tracker is empty. We
  push only for gateway/remote surfaces and ignore CLI-only requests.

* **complete** — the ``post_llm_call`` hook (``agent/conversation_loop.py``),
  fired once per turn after the tool-loop finishes and a final assistant
  response is produced, in BOTH CLI and gateway. We read ONLY ``session_id``
  from it (never ``user_message`` / ``assistant_response`` / ``conversation_history``).

* **error** — the ``on_session_end`` hook (``agent/conversation_loop.py``),
  fired at the end of every ``run_conversation()`` call. We push only on a
  genuine failure (``completed is False AND interrupted is False``); success is
  already covered by ``post_llm_call`` and interrupts are user-initiated.

* **clarify** — the ``pre_tool_call`` hook (``model_tools.py`` /
  ``agent/tool_executor.py``), an *observer* hook fired for EVERY tool call in
  BOTH CLI and gateway with ``tool_name`` / ``args`` / ``session_id``. We filter
  to ``tool_name == "clarify"`` (the clarify tool fires this BEFORE the user is
  prompted) and push a generic ``clarify``. We read ONLY ``session_id`` — never
  the clarify ``question`` / ``choices`` / args. The hook may return a block
  directive, but we are an observer and always return ``None``.

This module keeps the *pure* mapping (hook kwargs → payload) separate from any
I/O so the suite can assert payloads without a running agent.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# A push payload sink: receives one mapped payload, returns nothing. B4 (policy)
# and B5 (sender) compose behind this. For B3 the default is a no-op.
PushSink = Callable[[Dict[str, str]], None]


def _noop_sink(_payload: Dict[str, str]) -> None:
    """Default sink — drops payloads. Replaced by the policy/sender in B4/B5."""
    return None


# ---------------------------------------------------------------------------
# Current-turn session tracker
# ---------------------------------------------------------------------------
#
# The ``pre_approval_request`` hook carries NO ``session_id`` — only a
# source-derived ``session_key`` that diverges from the gateway
# ``session_id`` (== ``agent.session_id``) the iOS app opens chats by. But the
# plugin DOES see the real ``session_id`` earlier in the same turn
# (``pre_llm_call`` / ``pre_tool_call`` both fire before an approval). We stash
# it here so :func:`map_approval` can correlate the approval to the right chat.
#
# Belt-and-suspenders: we can't be sure whether the hooks run in the async
# context or a thread-pool worker, so we record into BOTH a ``ContextVar`` and a
# ``threading.local()`` and read the first non-empty on the way out.

_turn_session_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "hermes_push_turn_session", default=""
)
_turn_session_local = threading.local()


def record_turn_session(session_id: str) -> None:
    """Record the current turn's real ``session_id`` (both contextvar + thread-local).

    Called from ``pre_llm_call`` / ``pre_tool_call`` (which DO receive the real
    ``agent.session_id``). No-op on an empty id so a stray call can't wipe a good
    value mid-turn.
    """
    if not session_id:
        return
    sid = str(session_id)
    _turn_session_var.set(sid)
    _turn_session_local.session_id = sid


def current_turn_session() -> str:
    """Read the current turn's recorded ``session_id`` (contextvar → thread-local → "")."""
    sid = _turn_session_var.get()
    if sid:
        return sid
    return str(getattr(_turn_session_local, "session_id", "") or "")


def clear_turn_session() -> None:
    """Best-effort clear the turn tracker (hygiene; called on ``on_session_end``).

    The primary correctness mechanism is overwriting the value each turn via
    ``pre_llm_call``; this just avoids leaking a stale id across turns.
    """
    _turn_session_var.set("")
    _turn_session_local.session_id = ""


# ---------------------------------------------------------------------------
# Payload type tags + generic (content-free) copy
# ---------------------------------------------------------------------------

# The payload ``type`` values, matching the gateway-fn / iOS contract.
# ``clarify`` is produced from the ``pre_tool_call`` hook filtered to the clarify
# tool (see module docstring / :func:`map_clarify`).
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

# The tool whose ``pre_tool_call`` we treat as a clarify / input-needed event.
# ``pre_tool_call`` fires for EVERY tool, so we filter cheaply on this name first.
_CLARIFY_TOOL_NAME = "clarify"


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
    """Resolve the session id for a ``pre_approval_request`` hook.

    ``pre_approval_request`` carries NO ``session_id`` of its own — only a
    source-derived ``session_key`` that DIVERGES from the gateway ``session_id``
    (== ``agent.session_id``) the iOS app opens chats by. Using ``session_key``
    made the approval push carry the wrong id, so tapping it only landed on the
    sessions list. We resolve in this priority, first non-empty wins:

    1. :func:`current_turn_session` — the real ``session_id`` recorded earlier in
       the same turn by ``pre_llm_call`` / ``pre_tool_call`` (matches ``complete``).
    2. ``kwargs["session_id"]`` — in case a future host passes it directly.
    3. ``kwargs["session_key"]`` — last-resort fallback so nothing regresses if
       the turn tracker is somehow empty.

    Logs which source supplied the id (label + the non-secret session id only) so
    on-device misbehaviour is diagnosable from agent.log.
    """
    tracked = current_turn_session()
    if tracked:
        logger.info(
            "hermes-push: approval session id from turn-tracker (session_id=%s)",
            tracked,
        )
        return tracked

    explicit = str(kwargs.get("session_id") or "")
    if explicit:
        logger.info(
            "hermes-push: approval session id from session_id kwarg (session_id=%s)",
            explicit,
        )
        return explicit

    key = str(kwargs.get("session_key") or "")
    if key:
        logger.info(
            "hermes-push: approval session id from session_key fallback (session_id=%s)",
            key,
        )
    return key


def _hook_session_id(kwargs: Dict[str, Any]) -> str:
    """Pull ``session_id`` from a ``post_llm_call`` / ``on_session_end`` hook.

    Both fire with ``session_id=agent.session_id`` — the id the iOS app opens
    chats by and the WS frame carries, so push threads collapse correctly. (This
    is NOT the same as ``pre_approval_request``\\'s source-derived ``session_key``;
    see :func:`_approval_session_id`.) We read ONLY the id — never message content.
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


def map_clarify(**kwargs: Any) -> Optional[Dict[str, str]]:
    """Map a ``pre_tool_call`` hook invocation → a generic ``clarify`` payload.

    ``pre_tool_call`` fires for EVERY tool call (CLI + gateway), so we filter
    cheaply on ``tool_name == "clarify"`` first and skip everything else. The
    clarify tool fires this hook BEFORE the user is prompted — exactly the
    "input needed" moment we want to surface.

    Reads ONLY ``session_id`` — NEVER the clarify ``question`` / ``choices`` /
    ``args`` (privacy rule). Returns ``None`` for any non-clarify tool or when no
    session id is available.
    """
    if str(kwargs.get("tool_name") or "") != _CLARIFY_TOOL_NAME:
        return None
    session_id = _hook_session_id(kwargs)
    if not session_id:
        return None
    return make_payload(TYPE_CLARIFY, session_id)


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

    # -- clarify (pre_tool_call) hook ------------------------------------

    def on_pre_tool_call(self, **kwargs: Any) -> None:
        """``pre_tool_call`` hook callback (observer; returns None).

        Filters to the clarify tool and maps it to a generic ``clarify``
        payload, feeding the sink. Every other tool is ignored. Reads only
        ``session_id`` — never the clarify args. Always returns ``None`` so it
        can never block the tool (we are an observer, not a gatekeeper).

        Also records the turn's real ``session_id`` for the current-turn tracker
        so a following ``pre_approval_request`` (which lacks a usable id) can
        correlate to the right chat. This runs for EVERY tool, not just clarify.
        """
        record_turn_session(str(kwargs.get("session_id") or ""))
        self._emit_payload(map_clarify(**kwargs))
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
