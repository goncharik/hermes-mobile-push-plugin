"""Outbound POST to the push gateway (Task B5).

This is the *delivery* layer. Given a generic, content-free push payload that
already passed the suppression policy (B4), it fans the payload out to every
registered device, signs each copy with the single **shared** HMAC secret
(``HERMES_PUSH_HMAC_SECRET`` — the same value the gateway is configured with),
and POSTs it to the publisher's stateless push gateway (the only place the APNs
``.p8`` lives). The gateway forwards to APNs.

The gateway is stateless and verifies against ONE shared secret, so the plugin
must sign with that same shared secret (a per-device secret would never match).
If ``HERMES_PUSH_HMAC_SECRET`` is unset we send **unsigned** requests (the
gateway allows unsigned per ``validate.ts``) and log a one-time warning.

Hard requirements (from the plan + project conventions):

* **Never block the turn.** Delivery runs OFF the hook / WS thread on a small
  background executor (a daemon ``ThreadPoolExecutor``). The trigger pipeline
  calls :meth:`GatewaySender.send` and returns immediately; the POSTs happen
  later. A short per-request timeout plus a few bounded retries keep a slow or
  dead gateway from piling up work.

* **Byte-identical HMAC.** The gateway verifies the optional ``hmac`` over a
  canonical string that is ``JSON.stringify`` of the *signed* fields in a fixed
  key order — ``{device_token, apns_env, type, session_id, title, body,
  thread_id?}`` — with ``thread_id`` omitted entirely when absent (never
  ``null``) and ``hmac`` never part of the signed material. We reproduce that
  exactly with ``json.dumps(signed, separators=(",", ":"), ensure_ascii=False)``
  (verified byte-for-byte against the gateway's ``signHmac`` / ``canonicalSignedString``
  in ``gateway/src/validate.ts``). The digest is lowercase hex HMAC-SHA256 keyed
  by the shared ``HERMES_PUSH_HMAC_SECRET``.

* **Prune on 410.** When the gateway reports a device is gone (HTTP 410, relaying
  APNs Unregistered) we remove that token from the store so we stop signing for a
  dead device.

Everything that touches the network is behind an injectable :class:`HttpClient`
so tests run without a socket.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Tuple

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

# ---------------------------------------------------------------------------
# Shared HMAC secret
# ---------------------------------------------------------------------------
#
# The stateless gateway verifies the optional ``hmac`` against a SINGLE shared
# secret. The publisher provisions the SAME value here out-of-band (env var) and
# to the gateway. When unset, we sign nothing (the gateway allows unsigned) and
# warn once.
HMAC_SECRET_ENV = "HERMES_PUSH_HMAC_SECRET"

_warned_no_secret = False


def _shared_hmac_secret() -> str:
    """The shared HMAC secret from the environment, or ``""`` when unset.

    Read at call time (not import) so tests / late-set env are honored. Warns
    once when absent so an operator notices pushes go out unsigned.
    """
    global _warned_no_secret
    secret = (os.environ.get(HMAC_SECRET_ENV) or "").strip()
    if not secret and not _warned_no_secret:
        _warned_no_secret = True
        logger.warning(
            "hermes-push: %s is unset — sending UNSIGNED pushes. Set it to the "
            "same value configured on the gateway to enable HMAC verification.",
            HMAC_SECRET_ENV,
        )
    return secret

# The signed-field order MUST match gateway/src/validate.ts::canonicalSignedString.
# thread_id is appended only when present (it is omitted entirely when absent).
_SIGNED_FIELD_ORDER: Tuple[str, ...] = (
    "device_token",
    "apns_env",
    "type",
    "session_id",
    "title",
    "body",
)

# Delivery tunables (all overridable on the constructor).
DEFAULT_TIMEOUT_S = 5.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_S = 0.5


# ---------------------------------------------------------------------------
# Canonical signing (byte-identical to the gateway)
# ---------------------------------------------------------------------------


def canonical_signed_string(payload: Dict[str, Any]) -> str:
    """Build the canonical string the gateway HMACs over.

    Mirrors ``canonicalSignedString`` in ``gateway/src/validate.ts``: a compact
    JSON object of the signed fields in fixed order, with ``thread_id`` appended
    only when present (and non-empty), and never ``hmac``. ``ensure_ascii=False``
    + ``separators=(",", ":")`` make Python's output byte-identical to JS
    ``JSON.stringify`` (verified).
    """
    signed: Dict[str, str] = {key: str(payload.get(key, "")) for key in _SIGNED_FIELD_ORDER}
    thread_id = payload.get("thread_id")
    if thread_id is not None and thread_id != "":
        signed["thread_id"] = str(thread_id)
    return json.dumps(signed, separators=(",", ":"), ensure_ascii=False)


def compute_hmac(payload: Dict[str, Any], secret: str) -> str:
    """Lowercase-hex HMAC-SHA256 of the canonical string under ``secret``."""
    canonical = canonical_signed_string(payload)
    return hmac.new(
        secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


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

    Per device: build the full gateway request (the generic payload + the
    device's ``device_token`` / ``apns_env`` + an ``hmac`` keyed by the shared
    secret), POST it with a short timeout and a few bounded retries, and prune
    the token on a 410 / prune signal. All delivery runs on a background executor
    so the calling (hook / WS) thread is never blocked.
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

    def _build_request(self, payload: Dict[str, Any], record: Dict[str, Any]) -> Dict[str, Any]:
        """Assemble the full gateway request for one device (signed)."""
        device_token = str(record.get("device_token") or "")
        apns_env = str(record.get("apns_env") or "")
        request: Dict[str, Any] = {
            "device_token": device_token,
            "apns_env": apns_env,
            "type": payload.get("type"),
            "session_id": payload.get("session_id"),
            "title": payload.get("title"),
            "body": payload.get("body"),
        }
        thread_id = payload.get("thread_id")
        if thread_id is not None and thread_id != "":
            request["thread_id"] = thread_id
        secret = _shared_hmac_secret()
        if secret:
            request["hmac"] = compute_hmac(request, secret)
        return request

    def _deliver_one(self, payload: Dict[str, Any], record: Dict[str, Any]) -> None:
        """POST one device's signed request with timeout + bounded retries."""
        device_token = str(record.get("device_token") or "")
        if not device_token:
            return
        request = self._build_request(payload, record)
        body = json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        for attempt in range(1, self._max_attempts + 1):
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

            # Other non-2xx: retry transient (5xx) a few times; give up on 4xx.
            if resp.status < 500 or attempt >= self._max_attempts:
                logger.warning(
                    "hermes-push: gateway returned %d; giving up", resp.status
                )
                return
            self._backoff(attempt)

    def _backoff(self, attempt: int) -> None:
        """Exponential backoff between retries (attempt is 1-based)."""
        delay = self._backoff_base_s * (2 ** (attempt - 1))
        if delay > 0:
            self._sleep(delay)
