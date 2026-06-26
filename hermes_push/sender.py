"""Outbound POST to the push gateway (Task B5).

This is the *delivery* layer. Given a generic, content-free push payload that
already passed the suppression policy (B4), it fans the payload out to every
registered device and POSTs each copy to the publisher's stateless push gateway
(the only place the APNs ``.p8`` lives). The gateway forwards to APNs.

Auth model — gateway-issued device capability (NO shared secret)
----------------------------------------------------------------
The plugin holds **no shared secret**. Instead the gateway ISSUES a device-scoped
**capability** (an opaque hex string) that the plugin fetches once per device,
caches in the token store, and presents on every push. The plugin NEVER computes
the capability itself — it is opaque.

* ``POST <gateway>/register`` ``{"device_token": "<token>"}`` → 200
  ``{"capability": "<hex>"}``. The register URL is derived from the configured
  push URL (``…/push`` → ``…/register``).
* ``POST <gateway>/push`` includes a non-empty ``capability`` (and the generic
  payload fields) and NO ``hmac``. A 403 ``{"error":"invalid_capability"}`` means
  the stored capability is missing / stale / wrong: we drop it, re-register once,
  and retry the push a single time with the fresh capability.

Hard requirements (from the plan + project conventions):

* **Never block the turn.** Delivery runs OFF the hook / WS thread on a small
  background executor (a daemon ``ThreadPoolExecutor``). The trigger pipeline
  calls :meth:`GatewaySender.send` and returns immediately; the POSTs happen
  later. A short per-request timeout plus a few bounded retries keep a slow or
  dead gateway from piling up work.

* **Prune on 410.** When the gateway reports a device is gone (HTTP 410, relaying
  APNs Unregistered) we remove that token from the store so we stop pushing to a
  dead device.

Everything that touches the network is behind an injectable :class:`HttpClient`
so tests run without a socket.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol
from urllib.parse import urlsplit, urlunsplit

from hermes_push.store import TokenStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gateway URL
# ---------------------------------------------------------------------------
#
# Baked-in default. The publisher REPLACES this with the deployed gateway URL
# post-deploy (see the plan's Post-Completion / gateway deployment step). An env
# override (``HERMES_PUSH_GATEWAY_URL``) is honored so the URL can be pointed at
# a local mock / staging gateway for testing without editing source.
_DEFAULT_GATEWAY_URL = "https://hermes-push.example.workers.dev/push"
GATEWAY_URL = os.environ.get("HERMES_PUSH_GATEWAY_URL") or _DEFAULT_GATEWAY_URL

# Delivery tunables (all overridable on the constructor).
DEFAULT_TIMEOUT_S = 5.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_S = 0.5


def _register_url_for(push_url: str) -> str:
    """Derive the gateway's ``/register`` URL from its configured ``/push`` URL.

    If the push URL ends with ``/push`` we replace just that suffix with
    ``/register`` (preserving any path prefix). Otherwise we build
    ``<scheme>://<netloc>/register`` from the URL's origin.
    """
    if push_url.endswith("/push"):
        return push_url[: -len("/push")] + "/register"
    parts = urlsplit(push_url)
    return urlunsplit((parts.scheme, parts.netloc, "/register", "", ""))


# ---------------------------------------------------------------------------
# HTTP client seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpResponse:
    """Minimal response the sender needs: a status code and the raw body text."""

    status: int
    body: str


class HttpClient(Protocol):
    """The narrow HTTP surface the sender depends on (injectable for tests)."""

    def post_json(self, url: str, body: bytes, *, timeout: float) -> HttpResponse:
        ...


class UrllibHttpClient:
    """Default :class:`HttpClient` backed by the stdlib ``urllib`` (no new dep).

    A 4xx/5xx is returned as an :class:`HttpResponse` (via ``HTTPError``, which is
    itself a readable response) rather than raised, so the sender can branch on
    the status (e.g. 410 → prune) uniformly. Connection-level failures raise.
    """

    def post_json(self, url: str, body: bytes, *, timeout: float) -> HttpResponse:
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                text = resp.read().decode("utf-8", "replace")
                return HttpResponse(status=getattr(resp, "status", 200) or 200, body=text)
        except urllib.error.HTTPError as exc:  # 4xx/5xx — a readable response
            text = ""
            try:
                text = exc.read().decode("utf-8", "replace")
            except Exception:  # pragma: no cover — body already consumed
                pass
            return HttpResponse(status=exc.code, body=text)


# ---------------------------------------------------------------------------
# Prune-signal detection
# ---------------------------------------------------------------------------


def _is_prune(resp: HttpResponse) -> bool:
    """True when the gateway signals the device token should be pruned.

    Single signal: HTTP 410 (APNs Unregistered, relayed straight through by the
    gateway). We control both ends, so one unambiguous signal is enough.
    """
    return resp.status == 410


# ---------------------------------------------------------------------------
# The sender
# ---------------------------------------------------------------------------


class GatewaySender:
    """Fans a generic payload out to every registered device and POSTs each.

    Per device: ensure a gateway-issued capability is cached (fetch via
    ``/register`` if not), build the full gateway request (the generic payload +
    the device's ``device_token`` / ``apns_env`` + the ``capability``), POST it
    with a short timeout and a few bounded retries, prune the token on a 410, and
    on a 403 (stale capability) drop+re-fetch the capability and retry once. All
    delivery runs on a background executor so the calling (hook / WS) thread is
    never blocked.
    """

    def __init__(
        self,
        *,
        store: TokenStore,
        gateway_url: str = GATEWAY_URL,
        http_client: Optional[HttpClient] = None,
        executor: Optional[Executor] = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        sleep: Optional[Any] = None,
    ) -> None:
        self._store = store
        self._gateway_url = gateway_url
        self._register_url = _register_url_for(gateway_url)
        self._http = http_client or UrllibHttpClient()
        # A tiny daemon pool so a failure to join on shutdown never hangs the
        # agent. One worker is plenty — pushes are infrequent and we want them
        # ordered-ish, not concurrent floods at the gateway.
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="hermes-push-send"
        )
        self._timeout_s = float(timeout_s)
        self._max_attempts = max(1, int(max_attempts))
        self._backoff_base_s = float(backoff_base_s)
        self._sleep = sleep or time.sleep

    # -- public API -------------------------------------------------------

    def send(self, payload: Dict[str, Any]) -> None:
        """Queue ``payload`` for fan-out delivery and return immediately.

        Never blocks the caller: the actual POSTs run on the background
        executor. Safe to call from a hook / WS thread. A failure to even
        schedule is logged and swallowed (delivery is best-effort).
        """
        try:
            self._executor.submit(self._deliver_all, dict(payload))
        except Exception as exc:  # pragma: no cover — pool shutdown / saturated
            logger.warning("hermes-push: could not schedule push delivery: %s", exc)

    def send_blocking(self, payload: Dict[str, Any]) -> None:
        """Run the fan-out synchronously (used by tests; not on the hot path)."""
        self._deliver_all(dict(payload))

    def shutdown(self, *, wait: bool = False) -> None:
        """Stop the background executor (best-effort; called on plugin teardown)."""
        try:
            self._executor.shutdown(wait=wait)
        except Exception:  # pragma: no cover
            pass

    # -- delivery ---------------------------------------------------------

    def _deliver_all(self, payload: Dict[str, Any]) -> None:
        """Fan ``payload`` out to every registered device. Runs off-thread."""
        try:
            devices = self._store.list_all()
        except Exception as exc:  # pragma: no cover — store read failure
            logger.warning("hermes-push: could not list devices for push: %s", exc)
            return
        for record in devices:
            try:
                self._deliver_one(payload, record)
            except Exception as exc:  # never let one device sink the others
                logger.warning("hermes-push: push delivery error: %s", exc)

    def _ensure_capability(self, record: Dict[str, Any]) -> Optional[str]:
        """Return the device's gateway-issued capability, fetching it if needed.

        Uses the record's cached ``capability`` when present. Otherwise POSTs
        ``{"device_token": <token>}`` to the register URL, parses ``capability``
        from a 2xx response, persists it via ``store.set_capability``, and returns
        it. On any failure (non-2xx, network error, malformed body) returns
        ``None`` and logs a warning — the device is skipped this round and retried
        on the next push.
        """
        cached = record.get("capability")
        if isinstance(cached, str) and cached:
            return cached

        device_token = str(record.get("device_token") or "")
        if not device_token:
            return None

        body = json.dumps({"device_token": device_token}, separators=(",", ":")).encode("utf-8")
        try:
            resp = self._http.post_json(self._register_url, body, timeout=self._timeout_s)
        except Exception as exc:
            logger.warning("hermes-push: capability register failed (network): %s", exc)
            return None

        if not (200 <= resp.status < 300):
            logger.warning(
                "hermes-push: capability register returned %d; skipping device",
                resp.status,
            )
            return None

        try:
            capability = json.loads(resp.body or "").get("capability")
        except (json.JSONDecodeError, ValueError, AttributeError):
            logger.warning("hermes-push: capability register returned a malformed body")
            return None

        if not isinstance(capability, str) or not capability:
            logger.warning("hermes-push: capability register returned no capability")
            return None

        try:
            self._store.set_capability(device_token, capability)
        except Exception as exc:  # pragma: no cover — store write failure
            logger.warning("hermes-push: could not persist capability: %s", exc)
        return capability

    def _build_request(
        self, payload: Dict[str, Any], record: Dict[str, Any], capability: str
    ) -> Dict[str, Any]:
        """Assemble the full gateway request for one device (with capability)."""
        device_token = str(record.get("device_token") or "")
        apns_env = str(record.get("apns_env") or "")
        request: Dict[str, Any] = {
            "device_token": device_token,
            "apns_env": apns_env,
            "capability": capability,
            "type": payload.get("type"),
            "session_id": payload.get("session_id"),
            "title": payload.get("title"),
            "body": payload.get("body"),
        }
        thread_id = payload.get("thread_id")
        if thread_id is not None and thread_id != "":
            request["thread_id"] = thread_id
        return request

    def _deliver_one(self, payload: Dict[str, Any], record: Dict[str, Any]) -> None:
        """POST one device's request with timeout + bounded retries.

        Ensures a capability first (skipping the device when none is obtainable),
        then POSTs. A 403 (stale/rotated capability) triggers a single drop +
        re-fetch + retry; 410 prunes; 2xx is done; 5xx is retried (bounded); other
        4xx gives up.
        """
        device_token = str(record.get("device_token") or "")
        if not device_token:
            return

        capability = self._ensure_capability(record)
        if not capability:
            logger.info("hermes-push: no capability for device; skipping this round")
            return

        # `refreshed` bounds the 403 capability-refresh to a SINGLE re-fetch+retry
        # (never an unbounded loop), independent of the 5xx/transport retry budget.
        refreshed = False
        attempt = 1
        while attempt <= self._max_attempts:
            request = self._build_request(payload, record, capability)
            body = json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            try:
                resp = self._http.post_json(self._gateway_url, body, timeout=self._timeout_s)
            except Exception as exc:
                # Transport-level failure (timeout, connection refused, …).
                if attempt >= self._max_attempts:
                    logger.warning(
                        "hermes-push: push gave up after %d attempt(s): %s",
                        attempt,
                        exc,
                    )
                    return
                self._backoff(attempt)
                attempt += 1
                continue

            if _is_prune(resp):
                logger.info("hermes-push: gateway requested prune; removing device token")
                try:
                    self._store.prune_invalid(device_token)
                except Exception as exc:  # pragma: no cover
                    logger.warning("hermes-push: prune failed: %s", exc)
                return

            if 200 <= resp.status < 300:
                return

            # 403: stale/rotated capability. Drop it, re-fetch once, retry once —
            # without consuming the bounded retry budget (so it works even at
            # max_attempts=1). A second 403 gives up.
            if resp.status == 403:
                if refreshed:
                    logger.warning(
                        "hermes-push: capability still rejected after refresh; giving up"
                    )
                    return
                refreshed = True
                logger.info("hermes-push: capability rejected (403); refreshing")
                try:
                    self._store.set_capability(device_token, "")
                except Exception as exc:  # pragma: no cover
                    logger.warning("hermes-push: could not clear capability: %s", exc)
                record["capability"] = ""
                capability = self._ensure_capability(record)
                if not capability:
                    logger.warning("hermes-push: could not refresh capability; giving up")
                    return
                continue  # retry with the fresh capability (same attempt count)

            # Other non-2xx: retry transient (5xx) a few times; give up on 4xx.
            if resp.status < 500 or attempt >= self._max_attempts:
                logger.warning(
                    "hermes-push: gateway returned %d; giving up", resp.status
                )
                return
            self._backoff(attempt)
            attempt += 1

    def _backoff(self, attempt: int) -> None:
        """Exponential backoff between retries (attempt is 1-based)."""
        delay = self._backoff_base_s * (2 ** (attempt - 1))
        if delay > 0:
            self._sleep(delay)
