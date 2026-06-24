"""hermes-push plugin — push notifications for the Hermes Mobile iOS app.

Standalone, pip-installable Hermes Agent plugin. Uses the *public* plugin API
only (entry-point group ``hermes_agent.plugins`` → ``register(ctx)``); it makes
no changes to hermes-agent itself.

Triggers (all via REAL plugin hooks — see hermes-agent ``VALID_HOOKS`` in
``hermes_cli/plugins.py`` and the dispatch sites in
``agent/conversation_loop.py`` / ``tools/approval.py``)
-------------------------------------------------------------------------------
* **approval** — ``pre_approval_request`` (observer). Fires for CLI prompts and
  gateway/remote approvals; we push only for the gateway surface.
* **complete** — ``post_llm_call`` (observer). Fires once per *successful* turn,
  after the tool-loop finishes and a final assistant response is produced, in
  BOTH CLI and gateway. We build a generic ``complete`` payload (NO message
  content) and run it through the suppression policy (duration gate, dedup).
* **error** — ``on_session_end`` (observer). Fires at the end of EVERY
  ``run_conversation()`` call. We push an ``error`` only on a genuine failure
  (``completed is False AND interrupted is False``) — success is already covered
  by ``post_llm_call`` and interrupts are user-initiated.
* **clarify** — ``pre_tool_call`` (observer). Fires for EVERY tool call in BOTH
  CLI and gateway, with ``tool_name`` / ``args`` / ``session_id``. We filter to
  ``tool_name == "clarify"`` (the clarify tool fires this BEFORE the user is
  prompted — the "input needed" moment) and push a generic ``clarify`` (NO
  question / choices / args). Like approval, clarify is an attention-needed pause
  and is NOT duration-gated. We always return ``None`` (observer; never block).
* **turn-start** — ``pre_llm_call`` (observer). Fires once per turn *before* the
  tool-loop, in BOTH CLI and gateway, with ``session_id == agent.session_id``
  (the same key ``post_llm_call`` / ``on_session_end`` use). We record it as the
  policy's turn-start anchor so the complete-push duration gate can measure
  elapsed time. Cleared at ``on_session_end``. (If a turn somehow has no
  recorded start, the policy fails OPEN and notifies.)

Pipeline (wired in :func:`register`)
------------------------------------
    trigger hook
        → TriggerDispatcher.map_*  (generic, content-free payload)
        → SuppressionPolicy.decide (no-devices / live-client / duration / dedup)
        → GatewaySender.send       (per-device HMAC, off-thread POST, 410→prune)

Robustness
----------
Hooks are observer-style: every handler returns ``None`` and is wrapped so it can
never raise into the agent (the host also wraps callbacks). Pushing stays OFF the
turn's critical path — ``GatewaySender.send`` is fire-and-forget.
``register()`` wraps the sender / policy setup so a failure there can never break
plugin load — the worst case is "no pushes", never "agent won't start".
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from hermes_push import api
from hermes_push.policy import SuppressionPolicy
from hermes_push.sender import GatewaySender
from hermes_push.store import TokenStore
from hermes_push.triggers import TYPE_CLARIFY, TYPE_COMPLETE, TriggerDispatcher

logger = logging.getLogger(__name__)

# Lifecycle hooks this plugin observes. Names must match hermes-agent's
# ``VALID_HOOKS`` (hermes_cli/plugins.py).
#
# * pre_approval_request — approval needed. Observer hook; honors ``surface``
#                          (gateway → push, cli → skip).
# * pre_tool_call        — EVERY tool call. Observer hook; we filter to the
#                          clarify tool → ``clarify`` and always return None
#                          (never block).
# * pre_llm_call         — turn START. Observer hook; we record the policy's
#                          turn-start anchor and inject NO context (return None).
# * post_llm_call        — turn COMPLETE (success). Observer hook → ``complete``.
# * on_session_end       — turn ENDED. Observer hook → ``error`` on genuine
#                          failure; always clears the turn-start anchor.
_TRIGGER_HOOKS = (
    "pre_approval_request",
    "pre_tool_call",
    "pre_llm_call",
    "post_llm_call",
    "on_session_end",
)

# The shared dispatcher: owns the push sink and maps surfaced hook events →
# generic payloads. Its sink is set in :func:`register` to the policy+sender
# pipeline.
dispatcher = TriggerDispatcher()

# Wired in :func:`register`; module-level so hook handlers and tests can reach
# them. None until the plugin is registered.
_store: Optional[TokenStore] = None
_policy: Optional[SuppressionPolicy] = None
_sender: Optional[GatewaySender] = None


# ---------------------------------------------------------------------------
# Hook handlers
# ---------------------------------------------------------------------------
#
# Each handler accepts ``**kwargs`` so it never breaks on hook-signature changes,
# and returns ``None`` (observer-only) so it cannot alter agent flow. The host
# wraps every callback in its own try/except; we additionally keep these
# no-op-safe so a half-finished install can never disrupt a turn.


def _on_pre_llm_call(**kwargs: Any) -> None:
    """Turn START — record the policy's turn-start anchor for the session.

    Returns ``None`` so we inject no context into the user message (observer
    only). Best-effort + no-op-safe.

    CLI caveat: this fires in both CLI and gateway, so the duration gate works in
    both. Should a turn ever lack a recorded start, the policy fails OPEN.
    """
    if _policy is None:
        return None
    sid = kwargs.get("session_id") or kwargs.get("session_key")
    if sid:
        try:
            _policy.note_turn_start(str(sid))
        except Exception as exc:  # pragma: no cover — never reach the host
            logger.debug("hermes-push: note_turn_start failed: %s", exc)
    return None


def _on_session_end(**kwargs: Any) -> None:
    """Turn ENDED — emit an ``error`` push on genuine failure, then clear anchor.

    The error-vs-skip decision (only when ``completed is False AND interrupted is
    False``) lives in ``TriggerDispatcher.on_session_end`` / ``map_session_end``.
    We always clear the per-session turn-start anchor afterwards. Best-effort +
    no-op-safe.
    """
    try:
        dispatcher.on_session_end(**kwargs)
    except Exception as exc:  # pragma: no cover — dispatcher already swallows
        logger.debug("hermes-push: on_session_end dispatch failed: %s", exc)

    if _policy is not None:
        sid = kwargs.get("session_id") or kwargs.get("session_key")
        if sid:
            try:
                _policy.clear_turn_start(str(sid))
            except Exception as exc:  # pragma: no cover — never reach the host
                logger.debug("hermes-push: clear_turn_start failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Pipeline wiring
# ---------------------------------------------------------------------------


def _pipeline(payload: Dict[str, str]) -> None:
    """The dispatcher sink: policy.decide → (if send) sender.send. Never raises.

    Per-turn clarify suppression: a turn that already fired a ``clarify`` push
    has just pinged the user to come answer, so the trailing ``complete`` push is
    redundant. We skip ``complete`` outright (never even run it through the
    policy) when the session is marked, and mark the session whenever a
    ``clarify`` is decided to be sent.
    """
    if _policy is None or _sender is None:
        return

    payload_type = str(payload.get("type") or "")
    session_id = str(payload.get("session_id") or "")

    # complete: short-circuit if a clarify already notified this turn.
    if payload_type == TYPE_COMPLETE and session_id:
        try:
            if _policy.clarify_notified(session_id):
                logger.debug(
                    "hermes-push: suppressed complete (clarify already notified)"
                )
                _policy.clear_clarify_notified(session_id)
                return
        except Exception as exc:  # pragma: no cover — never break a turn
            logger.warning("hermes-push: clarify_notified check failed: %s", exc)

    try:
        decision = _policy.decide(payload)
    except Exception as exc:  # pragma: no cover — policy must never break a turn
        logger.warning("hermes-push: policy.decide failed: %s", exc)
        return
    if not decision.send:
        logger.debug("hermes-push: suppressed push (%s)", decision.reason)
        return

    # clarify: record that this turn pinged the user so the trailing complete is
    # suppressed. Mark only when actually decided-to-send (a suppressed clarify
    # must not suppress the complete).
    if payload_type == TYPE_CLARIFY and session_id:
        try:
            _policy.mark_clarify_notified(session_id)
        except Exception as exc:  # pragma: no cover — never break a turn
            logger.warning("hermes-push: mark_clarify_notified failed: %s", exc)

    _sender.send(payload)


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

_HOOK_HANDLERS = {
    "pre_approval_request": dispatcher.on_pre_approval_request,
    "pre_tool_call": dispatcher.on_pre_tool_call,
    "pre_llm_call": _on_pre_llm_call,
    "post_llm_call": dispatcher.on_post_llm_call,
    "on_session_end": _on_session_end,
}


def _wire_pipeline(ctx: Any) -> None:
    """Construct store/policy/sender and connect the pipeline.

    Isolated so :func:`register` can wrap it: any failure here is logged and the
    plugin still loads (hooks are registered separately and unconditionally).
    """
    global _store, _policy, _sender

    _store = TokenStore()
    # The PluginContext the host hands us does NOT expose the running gateway /
    # session map (it offers hook/tool/command registration, not the live server
    # objects), so we cannot introspect which sessions have a live client. We
    # therefore use the CONSERVATIVE default of "no client present" (always-notify)
    # — better to over-notify than to silently swallow a real event the user is
    # waiting on. The suppression still has teeth: the duration gate + dedup +
    # no-devices gates all remain fully active regardless.
    _policy = SuppressionPolicy(
        client_present=lambda _sid: False,
        device_count=lambda: len(_store.list_all()) if _store is not None else 0,
    )
    _sender = GatewaySender(store=_store)

    # Point the REST module at the same store + sender so /register, /unregister
    # and /test all share one instance (the /test route reuses this very sender,
    # bypassing the suppression policy — a test push is an explicit user action).
    api.set_store(_store)
    api.set_sender(_sender)

    # The dispatcher hands every mapped payload to the policy+sender pipeline.
    dispatcher.set_sink(_pipeline)


def register(ctx) -> None:
    """Wire the plugin into the Hermes Agent host.

    Called once at plugin load with a ``PluginContext``. Registers the trigger
    hooks and stands up the policy/sender pipeline. The REST router is mounted
    separately by the dashboard plugin system via ``dashboard/manifest.json``
    (``api`` field) — not through ``ctx``.
    """
    for hook_name, handler in _HOOK_HANDLERS.items():
        ctx.register_hook(hook_name, handler)
    logger.debug("hermes-push: registered %d hook(s)", len(_HOOK_HANDLERS))

    # Pipeline setup is best-effort: a failure here must never break plugin load.
    try:
        _wire_pipeline(ctx)
    except Exception as exc:
        logger.warning(
            "hermes-push: pipeline setup failed (%s); pushes disabled, agent "
            "unaffected.",
            exc,
        )
