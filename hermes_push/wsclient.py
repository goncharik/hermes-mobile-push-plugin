"""Guarded, off-thread loopback ``/api/ws`` connector (Task B5).

Background
----------
Three of the four triggers — turn **complete**, **error**, and **clarify** — are
emitted by the agent's gateway via ``tui_gateway/server.py::_emit`` straight to
the WebSocket *transport bound to that session's request context* (verified:
``write_json`` routes to ``current_transport()`` or the session's bound
transport — there is no global broadcast and no registerable hook for these
events). The fourth, **approval**, has a real ``pre_approval_request`` hook and
is wired directly in ``__init__`` — it does NOT need this connector.

Architectural limitation (documented honestly)
----------------------------------------------
Because ``_emit`` is transport-scoped, a *separate* loopback ``/api/ws``
connection opened by this plugin gets its **own** session/transport and does NOT
receive complete/error/clarify events for the user's *other* sessions. So this
connector cannot, with hermes-agent as it stands today, observe the events it
would need to fire complete/error/clarify pushes for arbitrary sessions.

To fully support those three triggers, hermes-agent would need a global event
fan-out (e.g. an ``/api/events``-style broadcast of every ``_emit`` across all
sessions, or a plugin hook on ``_emit``). Until then:

* **approval** pushes work (via the hook).
* **complete / error / clarify** pushes are best-effort: this connector is built
  and runs, mapping any qualifying frames it *does* receive into the dispatcher
  (so the moment hermes-agent grows a broadcast channel, this lights up), but in
  practice it observes nothing for other sessions today.

What this connector guarantees regardless
------------------------------------------
* It is OFF the hook / turn thread (its own daemon thread + asyncio loop).
* It NEVER blocks ``register()`` and NEVER raises into the host.
* It degrades to a no-op (log + keep the plugin working) if the WS is
  unreachable, ``websockets`` isn't installed, or auth fails.
* It only reads ``type`` + ``session_id`` from frames — never content.

Connection / auth
-----------------
Loopback / ``--insecure`` mode authenticates with ``?token=<session-token>``
(``_ws_auth_reason`` in ``hermes_cli/web_server.py``), where the token is the
process's ``HERMES_DASHBOARD_SESSION_TOKEN``. The URL is built from env:

* ``HERMES_PUSH_WS_URL`` — full override (e.g. ``ws://127.0.0.1:8080/api/ws``),
  otherwise built from ``HERMES_DASHBOARD_URL`` /
  ``HERMES_DASHBOARD_HOST`` + ``HERMES_DASHBOARD_PORT`` (defaults
  ``127.0.0.1:8080``).
* ``HERMES_DASHBOARD_SESSION_TOKEN`` — appended as ``?token=`` when present.

(Gated mode uses single-use ``?ticket=`` / process-internal ``?internal=``
credentials the plugin cannot mint from outside the server process, so the
loopback connector only attempts the token path; in gated mode it degrades
gracefully.)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import urllib.parse
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Event ``type`` strings that signal a turn started (feeds policy.note_turn_start)
# vs ended (feeds clear_turn_start). complete/error end a turn; message.start
# begins one. Kept here so the connector owns the turn-lifecycle signalling.
_TURN_START_EVENTS = frozenset({"message.start"})
_TURN_END_EVENTS = frozenset({"message.complete", "error"})

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = "8080"


def build_ws_url() -> str:
    """Resolve the loopback ``/api/ws`` URL (with ``?token=`` when available).

    Always resolves to a URL (a host is determined from the env or the loopback
    defaults). The connector's ``url_factory`` seam stays ``Optional`` so an
    injected factory can still opt out.
    """
    explicit = (os.environ.get("HERMES_PUSH_WS_URL") or "").strip()
    if explicit:
        base = explicit
    else:
        dashboard_url = (os.environ.get("HERMES_DASHBOARD_URL") or "").strip()
        if dashboard_url:
            parsed = urllib.parse.urlparse(dashboard_url)
            scheme = "wss" if parsed.scheme == "https" else "ws"
            netloc = parsed.netloc or f"{_DEFAULT_HOST}:{_DEFAULT_PORT}"
        else:
            host = (os.environ.get("HERMES_DASHBOARD_HOST") or _DEFAULT_HOST).strip()
            port = (os.environ.get("HERMES_DASHBOARD_PORT") or _DEFAULT_PORT).strip()
            scheme = "ws"
            netloc = f"{host}:{port}"
        base = f"{scheme}://{netloc}/api/ws"

    token = (os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN") or "").strip()
    if token and "token=" not in base:
        sep = "&" if "?" in base else "?"
        base = f"{base}{sep}{urllib.parse.urlencode({'token': token})}"
    return base


# A callback fed each decoded event frame (the dispatcher's handle_ws_event).
FrameSink = Callable[[dict], None]
# Turn-lifecycle callbacks (policy.note_turn_start / clear_turn_start).
TurnSignal = Callable[[str], None]


class LoopbackWsConnector:
    """Best-effort background WS reader. Never blocks or raises into the host.

    Start it with :meth:`start` (returns immediately); it spins a daemon thread
    that runs its own asyncio loop, connects, and feeds frames to the sink. Stop
    it with :meth:`stop`. If anything fails it logs and the plugin keeps working
    (approval-via-hook is unaffected).
    """

    def __init__(
        self,
        *,
        on_frame: FrameSink,
        on_turn_start: Optional[TurnSignal] = None,
        on_turn_end: Optional[TurnSignal] = None,
        url_factory: Callable[[], Optional[str]] = build_ws_url,
        connect: Optional[Callable[..., Any]] = None,
        reconnect_delay_s: float = 5.0,
    ) -> None:
        self._on_frame = on_frame
        self._on_turn_start = on_turn_start
        self._on_turn_end = on_turn_end
        self._url_factory = url_factory
        # Injectable so tests supply a fake WS without ``websockets`` installed.
        self._connect = connect
        self._reconnect_delay_s = float(reconnect_delay_s)
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop = threading.Event()

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Spawn the daemon reader thread. Returns immediately; never raises."""
        if self._thread is not None:
            return
        try:
            self._thread = threading.Thread(
                target=self._run,
                name="hermes-push-ws",
                daemon=True,
            )
            self._thread.start()
        except Exception as exc:  # pragma: no cover — thread spawn failure
            logger.warning("hermes-push: WS connector failed to start: %s", exc)

    def stop(self, *, timeout: float = 2.0) -> None:
        """Signal the reader to stop and (best-effort) join it."""
        self._stop.set()
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(lambda: None)
            except Exception:  # pragma: no cover
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    # -- internals --------------------------------------------------------

    def _resolve_connect(self) -> Optional[Callable[..., Any]]:
        if self._connect is not None:
            return self._connect
        try:
            import websockets  # type: ignore

            return websockets.connect
        except Exception:
            logger.info(
                "hermes-push: 'websockets' not available; loopback WS connector "
                "disabled (approval pushes still work via the hook)."
            )
            return None

    def _run(self) -> None:
        connect = self._resolve_connect()
        if connect is None:
            return
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._reader_loop(connect))
        except Exception as exc:  # pragma: no cover — never propagate
            logger.warning("hermes-push: WS connector stopped on error: %s", exc)
        finally:
            try:
                if self._loop is not None:
                    self._loop.close()
            except Exception:  # pragma: no cover
                pass

    async def _reader_loop(self, connect: Callable[..., Any]) -> None:
        """Connect → read frames → reconnect, until stopped. Degrades on error."""
        while not self._stop.is_set():
            url = self._url_factory()
            if not url:
                logger.info("hermes-push: no loopback WS URL; connector idle.")
                return
            try:
                async with connect(url) as ws:
                    await self._consume(ws)
            except Exception as exc:
                logger.info("hermes-push: loopback WS unavailable (%s); retrying.", exc)
            if self._stop.is_set():
                return
            await asyncio.sleep(self._reconnect_delay_s)

    async def _consume(self, ws: Any) -> None:
        """Read frames from one connection until it drops or we're stopped.

        Each received frame is handled before the stop check so a frame that
        arrives concurrently with a stop request is not dropped.
        """
        async for raw in ws:
            self._handle_raw(raw)
            if self._stop.is_set():
                return

    def _handle_raw(self, raw: Any) -> None:
        """Decode one raw WS message and route it. Never raises."""
        try:
            frame = json.loads(raw)
        except (json.JSONDecodeError, ValueError, TypeError):
            return
        if not isinstance(frame, dict):
            return
        params = frame.get("params") if isinstance(frame.get("params"), dict) else frame
        event_type = str(params.get("type") or "")
        session_id = str(params.get("session_id") or "")

        # Turn-lifecycle signals feed the suppression policy's duration gate.
        if event_type in _TURN_START_EVENTS and self._on_turn_start is not None:
            self._safe(self._on_turn_start, session_id)
        elif event_type in _TURN_END_EVENTS and self._on_turn_end is not None:
            self._safe(self._on_turn_end, session_id)

        # Feed the frame to the dispatcher (it maps complete/error/clarify only).
        self._safe(self._on_frame, frame)

    @staticmethod
    def _safe(fn: Callable[..., Any], arg: Any) -> None:
        try:
            fn(arg)
        except Exception as exc:  # never let a callback reach the reader loop
            logger.warning("hermes-push: WS callback error: %s", exc)
