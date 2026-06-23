"""hermes-push plugin — push notifications for the Hermes Mobile iOS app.

Standalone, pip-installable Hermes Agent plugin. Uses the *public* plugin API
only (entry-point group ``hermes_agent.plugins`` → ``register(ctx)``); it makes
no changes to hermes-agent itself.

Architecture (see docs/plans/2026-06-20-push-notifications.md in the
hermes-mobile repo):

* This plugin runs on the user's own agent. It registers lifecycle hooks that
  fire when the agent needs the user (approval requests) and — for the events
  that have no hook (turn complete, error, clarify) — a best-effort, guarded,
  off-thread loopback ``/api/ws`` connector. A REST route (``api.py``) lets the
  iOS app register its APNs device token.
* When a trigger fires it builds a generic ``{type, session_id, title, body}``
  payload (no message content), runs it through the suppression policy
  (``policy.py``), and — if it should send — POSTs it (signed, per device) to a
  tiny stateless push gateway the *publisher* operates (the only place the APNs
  ``.p8`` key lives). The gateway forwards to APNs.

Pipeline (wired in :func:`register`)
------------------------------------
    trigger (approval hook / loopback WS)
        → TriggerDispatcher.make_payload (generic, content-free)
        → SuppressionPolicy.decide  (no-devices / live-client / duration / dedup)
        → GatewaySender.send        (per-device HMAC, off-thread POST, 410→prune)

Robustness
----------
``register()`` wraps the sender / policy / WS setup so a failure there can never
break plugin load — the worst case is "no pushes", never "agent won't start".
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from hermes_push import api
from hermes_push.policy import SuppressionPolicy
from hermes_push.sender import GatewaySender
from hermes_push.store import TokenStore
from hermes_push.triggers import TriggerDispatcher
from hermes_push.wsclient import LoopbackWsConnector

logger = logging.getLogger(__name__)

# Lifecycle hooks this plugin observes. Names must match hermes-agent's
# ``VALID_HOOKS`` (hermes_cli/plugins.py).
#
# * pre_approval_request   — approval needed. Observer-only hook; the ONLY one of
#                            the four triggers that has a real hook path. Honors
#                            ``surface`` (gateway → push, cli → skip).
# * pre_gateway_dispatch   — observer over *incoming* user messages only. It does
#                            NOT see outbound events, so turn-complete / error /
#                            clarify do NOT arrive here. Kept as a strict no-op
#                            observer.
# * on_session_end         — turn finished; a cheap completion signal we use to
#                            clear the policy's per-session turn-start anchor.
_TRIGGER_HOOKS = (
    "pre_approval_request",
    "pre_gateway_dispatch",
    "on_session_end",
)

# The shared dispatcher: owns the push sink and maps surfaced events → generic
# payloads. Its sink is set in :func:`register` to the policy+sender pipeline.
dispatcher = TriggerDispatcher()

# Wired in :func:`register`; module-level so hook handlers and tests can reach
# them. None until the plugin is registered.
_store: Optional[TokenStore] = None
_policy: Optional[SuppressionPolicy] = None
_sender: Optional[GatewaySender] = None
_ws_connector: Optional[LoopbackWsConnector] = None


# ---------------------------------------------------------------------------
# Hook handlers
# ---------------------------------------------------------------------------
#
# Each handler accepts ``**kwargs`` so it never breaks on hook-signature changes,
# and returns ``None`` (observer-only) so it cannot alter agent flow. The host
# wraps every callback in its own try/except; we additionally keep these
# no-op-safe so a half-finished install can never disrupt a turn.


def _on_pre_gateway_dispatch(**_: Any) -> Optional[Dict[str, str]]:
    """Gateway pre-dispatch. Observer-only — must NOT influence flow.

    Returning ``None`` (never an ``{"action": ...}`` dict) guarantees the plugin
    can never skip/rewrite a user message. This hook only sees *incoming* user
    messages; the turn-complete / error / clarify triggers do NOT pass through
    here (they go through the loopback WS connector).
    """
    return None


def _on_session_end(**kwargs: Any) -> None:
    """Session/turn ended — clear the policy's turn-start anchor for the session.

    Best-effort + no-op-safe: a missing policy or unknown session is fine.
    """
    if _policy is None:
        return None
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
    """The dispatcher sink: policy.decide → (if send) sender.send. Never raises."""
    if _policy is None or _sender is None:
        return
    try:
        decision = _policy.decide(payload)
    except Exception as exc:  # pragma: no cover — policy must never break a turn
        logger.warning("hermes-push: policy.decide failed: %s", exc)
        return
    if not decision.send:
        logger.debug("hermes-push: suppressed push (%s)", decision.reason)
        return
    _sender.send(payload)


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

_HOOK_HANDLERS = {
    "pre_approval_request": dispatcher.on_pre_approval_request,
    "pre_gateway_dispatch": _on_pre_gateway_dispatch,
    "on_session_end": _on_session_end,
}


def _wire_pipeline(ctx: Any) -> None:
    """Construct store/policy/sender, connect the pipeline, start the WS reader.

    Isolated so :func:`register` can wrap it: any failure here is logged and the
    plugin still loads (hooks are registered separately and unconditionally).
    """
    global _store, _policy, _sender, _ws_connector

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

    # Best-effort loopback WS connector for complete/error/clarify (see
    # wsclient.py for the architectural limitation — these events are
    # transport-scoped in hermes-agent today, so this observes nothing for other
    # sessions until the agent grows a global event broadcast; it never blocks or
    # breaks the plugin).
    _ws_connector = LoopbackWsConnector(
        on_frame=dispatcher.handle_ws_event,
        on_turn_start=_policy.note_turn_start,
        on_turn_end=_policy.clear_turn_start,
    )
    _ws_connector.start()


def register(ctx) -> None:
    """Wire the plugin into the Hermes Agent host.

    Called once at plugin load with a ``PluginContext``. Registers trigger hooks
    and stands up the policy/sender/WS pipeline. The REST router is mounted
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
