"""Tests for the outbound gateway sender (Task B5).

Mocks the HTTP client so nothing hits the network. Covers payload shape,
byte-identical HMAC (shared secret), timeout/retry, prune (410) → token removed,
multi-device fan-out, and off-thread (non-blocking) dispatch.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from concurrent.futures import Executor
from typing import Any, Callable, List, Optional

import pytest

from hermes_push import sender as sender_mod
from hermes_push.sender import (
    HMAC_SECRET_ENV,
    GatewaySender,
    HttpResponse,
    canonical_signed_string,
    compute_hmac,
)
from hermes_push.store import TokenStore


# The shared HMAC secret the plugin signs with (provisioned via env, matching the gateway).
SHARED_SECRET = "shared-secret-for-tests"


@pytest.fixture(autouse=True)
def _shared_hmac_secret(monkeypatch):
    """Provision the shared HMAC secret for every sender test and reset the warn flag."""
    monkeypatch.setenv(HMAC_SECRET_ENV, SHARED_SECRET)
    monkeypatch.setattr(sender_mod, "_warned_no_secret", False)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeHttp:
    """Records POSTs and returns scripted responses (or raises) per call."""

    def __init__(self, responses: Optional[List[Any]] = None) -> None:
        # Each item is an HttpResponse to return or an Exception to raise.
        self._responses = list(responses or [])
        self.calls: List[dict] = []

    def post_json(self, url: str, body: bytes, *, timeout: float) -> HttpResponse:
        self.calls.append(
            {"url": url, "body": json.loads(body.decode("utf-8")), "timeout": timeout}
        )
        if self._responses:
            item = self._responses.pop(0)
        else:
            item = HttpResponse(status=200, body="")
        if isinstance(item, Exception):
            raise item
        return item


class InlineExecutor(Executor):
    """Runs submitted work synchronously so fan-out is deterministic in tests."""

    def submit(self, fn, /, *args, **kwargs):  # type: ignore[override]
        fn(*args, **kwargs)
        return None


def _store(tmp_path) -> TokenStore:
    return TokenStore(base_dir=tmp_path)


def _make_sender(store: TokenStore, http: FakeHttp, **kw) -> GatewaySender:
    return GatewaySender(
        store=store,
        gateway_url="https://gw.example/push",
        http_client=http,
        executor=InlineExecutor(),
        backoff_base_s=0.0,  # no real sleeping in tests
        sleep=lambda _s: None,
        **kw,
    )


GENERIC_PAYLOAD = {
    "type": "complete",
    "session_id": "sess-1",
    "title": "Turn complete",
    "body": "Hermes finished the turn",
    "thread_id": "sess-1",
}


# ---------------------------------------------------------------------------
# Canonical HMAC (must match gateway/src/validate.ts byte-for-byte)
# ---------------------------------------------------------------------------


def test_canonical_string_key_order_and_compact_separators():
    request = {
        "device_token": "dt",
        "apns_env": "production",
        "type": "complete",
        "session_id": "s1",
        "title": "Turn complete",
        "body": "Hermes finished the turn",
        "thread_id": "s1",
        "hmac": "should-not-be-signed",
    }
    canonical = canonical_signed_string(request)
    # Exact string the gateway's JSON.stringify produces (fixed key order,
    # compact separators, hmac excluded, thread_id last when present).
    assert canonical == (
        '{"device_token":"dt","apns_env":"production","type":"complete",'
        '"session_id":"s1","title":"Turn complete",'
        '"body":"Hermes finished the turn","thread_id":"s1"}'
    )
    assert "hmac" not in canonical


def test_canonical_string_omits_thread_id_when_absent():
    request = {
        "device_token": "dt",
        "apns_env": "sandbox",
        "type": "approval",
        "session_id": "s1",
        "title": "Approval needed",
        "body": "Hermes needs your approval",
    }
    canonical = canonical_signed_string(request)
    assert "thread_id" not in canonical
    assert canonical.endswith('"body":"Hermes needs your approval"}')


def test_canonical_string_non_ascii_is_not_unicode_escaped():
    # JS JSON.stringify emits raw UTF-8, not \uXXXX — Python must match.
    request = {
        "device_token": "dt",
        "apns_env": "sandbox",
        "type": "error",
        "session_id": "s1",
        "title": "café — ünî",
        "body": "x",
    }
    canonical = canonical_signed_string(request)
    assert "café — ünî" in canonical
    assert "\\u" not in canonical


def test_compute_hmac_matches_independent_recompute():
    secret = "0" * 64
    request = {
        "device_token": "dt",
        "apns_env": "production",
        "type": "complete",
        "session_id": "s1",
        "title": "Turn complete",
        "body": "Hermes finished the turn",
        "thread_id": "s1",
    }
    expected = hmac.new(
        secret.encode("utf-8"),
        canonical_signed_string(request).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert compute_hmac(request, secret) == expected
    # Lowercase hex digest of the right length.
    assert expected == expected.lower()
    assert len(expected) == 64


# ---------------------------------------------------------------------------
# Payload shape + HMAC on the wire
# ---------------------------------------------------------------------------


def test_send_posts_expected_payload_shape_with_hmac(tmp_path):
    store = _store(tmp_path)
    store.upsert(
        device_token="dt-1",
        apns_env="production",
        app_version="1.0",
    )
    http = FakeHttp([HttpResponse(status=200, body='{"ok":true}')])
    sender = _make_sender(store, http)

    sender.send(GENERIC_PAYLOAD)

    assert len(http.calls) == 1
    sent = http.calls[0]["body"]
    assert sent["device_token"] == "dt-1"
    assert sent["apns_env"] == "production"
    assert sent["type"] == "complete"
    assert sent["session_id"] == "sess-1"
    assert sent["title"] == "Turn complete"
    assert sent["body"] == "Hermes finished the turn"
    assert sent["thread_id"] == "sess-1"
    # HMAC present and matches a recompute over the signed fields with the shared secret.
    assert sent["hmac"] == compute_hmac(sent, SHARED_SECRET)
    assert http.calls[0]["url"] == "https://gw.example/push"
    assert http.calls[0]["timeout"] == pytest.approx(5.0)


def test_send_unsigned_when_shared_secret_unset(tmp_path, monkeypatch):
    # No shared secret configured → the gateway allows unsigned, so we omit `hmac`.
    monkeypatch.delenv(HMAC_SECRET_ENV, raising=False)
    store = _store(tmp_path)
    store.upsert(device_token="dt-1", apns_env="production", app_version="1.0")
    http = FakeHttp([HttpResponse(status=200, body='{"ok":true}')])
    sender = _make_sender(store, http)

    sender.send(GENERIC_PAYLOAD)

    sent = http.calls[0]["body"]
    assert "hmac" not in sent


def test_send_omits_thread_id_when_absent(tmp_path):
    store = _store(tmp_path)
    store.upsert(
        device_token="dt-1", apns_env="sandbox", app_version="1.0"
    )
    http = FakeHttp()
    sender = _make_sender(store, http)

    payload = {
        "type": "approval",
        "session_id": "s1",
        "title": "Approval needed",
        "body": "Hermes needs your approval",
    }
    sender.send(payload)

    sent = http.calls[0]["body"]
    assert "thread_id" not in sent


def test_no_devices_means_no_post(tmp_path):
    store = _store(tmp_path)  # empty
    http = FakeHttp()
    sender = _make_sender(store, http)
    sender.send(GENERIC_PAYLOAD)
    assert http.calls == []


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------


def test_multi_device_fanout_each_signed_with_shared_secret(tmp_path):
    store = _store(tmp_path)
    store.upsert(
        device_token="dt-A", apns_env="production", app_version="1"
    )
    store.upsert(
        device_token="dt-B", apns_env="sandbox", app_version="1"
    )
    http = FakeHttp(
        [HttpResponse(status=200, body=""), HttpResponse(status=200, body="")]
    )
    sender = _make_sender(store, http)

    sender.send(GENERIC_PAYLOAD)

    assert len(http.calls) == 2
    by_token = {c["body"]["device_token"]: c["body"] for c in http.calls}
    assert set(by_token) == {"dt-A", "dt-B"}
    assert by_token["dt-A"]["apns_env"] == "production"
    assert by_token["dt-B"]["apns_env"] == "sandbox"
    # Every device is signed with the SAME shared secret (per-device fields still differ).
    assert by_token["dt-A"]["hmac"] == compute_hmac(by_token["dt-A"], SHARED_SECRET)
    assert by_token["dt-B"]["hmac"] == compute_hmac(by_token["dt-B"], SHARED_SECRET)


def test_one_device_failure_does_not_block_others(tmp_path):
    store = _store(tmp_path)
    store.upsert(device_token="dt-A", apns_env="production", app_version="1")
    store.upsert(device_token="dt-B", apns_env="production", app_version="1")
    # First device: persistent transport error (exhausts retries). Second: ok.
    http = FakeHttp(
        [
            ConnectionError("boom"),
            ConnectionError("boom"),
            ConnectionError("boom"),
            HttpResponse(status=200, body=""),
        ]
    )
    sender = _make_sender(store, http, max_attempts=3)

    sender.send(GENERIC_PAYLOAD)

    # 3 failed attempts for the first device, then 1 success for the second.
    assert len(http.calls) == 4
    assert http.calls[-1]["body"]["device_token"] in {"dt-A", "dt-B"}


# ---------------------------------------------------------------------------
# Timeout / retry
# ---------------------------------------------------------------------------


def test_transient_failure_then_success_retries(tmp_path):
    store = _store(tmp_path)
    store.upsert(device_token="dt", apns_env="production", app_version="1")
    http = FakeHttp([TimeoutError("slow"), HttpResponse(status=200, body="")])
    sender = _make_sender(store, http, max_attempts=3)

    sender.send(GENERIC_PAYLOAD)

    assert len(http.calls) == 2  # one retry, then success


def test_gives_up_after_max_attempts(tmp_path):
    store = _store(tmp_path)
    store.upsert(device_token="dt", apns_env="production", app_version="1")
    http = FakeHttp([TimeoutError("x"), TimeoutError("x"), TimeoutError("x")])
    sender = _make_sender(store, http, max_attempts=3)

    sender.send(GENERIC_PAYLOAD)

    assert len(http.calls) == 3  # exactly max_attempts, then give up


def test_5xx_is_retried_4xx_is_not(tmp_path):
    store = _store(tmp_path)
    store.upsert(device_token="dt", apns_env="production", app_version="1")
    # 503 (retry) then 400 (give up immediately).
    http = FakeHttp([HttpResponse(status=503, body=""), HttpResponse(status=400, body="bad")])
    sender = _make_sender(store, http, max_attempts=5)

    sender.send(GENERIC_PAYLOAD)

    assert len(http.calls) == 2  # 503 retried once, 400 gives up


# ---------------------------------------------------------------------------
# Prune (HTTP 410 — the single prune signal)
# ---------------------------------------------------------------------------


def test_410_prunes_token(tmp_path):
    store = _store(tmp_path)
    store.upsert(device_token="dead", apns_env="production", app_version="1")
    http = FakeHttp(
        [HttpResponse(status=410, body='{"device_token":"dead"}')]
    )
    sender = _make_sender(store, http)

    sender.send(GENERIC_PAYLOAD)

    assert store.get("dead") is None
    assert len(http.calls) == 1  # no retry after a prune


def test_non_410_status_does_not_prune(tmp_path):
    # 410 is the ONLY prune signal — a body alone on a non-410 status must NOT prune.
    store = _store(tmp_path)
    store.upsert(device_token="alive", apns_env="production", app_version="1")
    http = FakeHttp([HttpResponse(status=200, body='{"device_token":"alive"}')])
    sender = _make_sender(store, http)

    sender.send(GENERIC_PAYLOAD)

    assert store.get("alive") is not None


def test_success_does_not_prune(tmp_path):
    store = _store(tmp_path)
    store.upsert(device_token="live", apns_env="production", app_version="1")
    http = FakeHttp([HttpResponse(status=200, body='{"ok":true}')])
    sender = _make_sender(store, http)

    sender.send(GENERIC_PAYLOAD)

    assert store.get("live") is not None


# ---------------------------------------------------------------------------
# Off-thread: send() must not block
# ---------------------------------------------------------------------------


class _RecordingExecutor(Executor):
    """Captures submitted work without running it (proves dispatch, no block)."""

    def __init__(self) -> None:
        self.submitted: List[tuple] = []

    def submit(self, fn, /, *args, **kwargs):  # type: ignore[override]
        self.submitted.append((fn, args, kwargs))
        return None


def test_send_dispatches_to_executor_and_does_not_run_inline(tmp_path):
    store = _store(tmp_path)
    store.upsert(device_token="dt", apns_env="production", app_version="1")
    http = FakeHttp()
    rec = _RecordingExecutor()
    sender = GatewaySender(
        store=store, gateway_url="https://gw/push", http_client=http, executor=rec
    )

    sender.send(GENERIC_PAYLOAD)

    # Work was handed to the executor, NOT run inline (no HTTP call yet).
    assert len(rec.submitted) == 1
    assert http.calls == []
    # Running the captured work performs the POST.
    fn, args, kwargs = rec.submitted[0]
    fn(*args, **kwargs)
    assert len(http.calls) == 1


def test_send_returns_quickly_with_slow_http(tmp_path):
    """A slow gateway must not block the calling (hook/turn) thread."""
    store = _store(tmp_path)
    store.upsert(device_token="dt", apns_env="production", app_version="1")

    started = threading.Event()
    release = threading.Event()

    class SlowHttp:
        def post_json(self, url, body, *, timeout):
            started.set()
            release.wait(timeout=5)
            return HttpResponse(status=200, body="")

    # Real (default) thread-pool executor here so delivery is genuinely off-thread.
    sender = GatewaySender(
        store=store, gateway_url="https://gw/push", http_client=SlowHttp()
    )

    t0 = time.monotonic()
    sender.send(GENERIC_PAYLOAD)
    elapsed = time.monotonic() - t0

    # send() returned essentially immediately despite the blocking POST.
    assert elapsed < 0.5
    assert started.wait(timeout=2), "delivery never started on the background thread"
    release.set()
    sender.shutdown(wait=True)
