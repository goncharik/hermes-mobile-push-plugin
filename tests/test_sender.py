"""Tests for the outbound gateway sender (Task B5).

Mocks the HTTP client so nothing hits the network. Covers the register-URL
derivation, fetching + caching the gateway-issued device capability, payload
shape (capability present, NO hmac), the 403 capability-refresh, timeout/retry,
prune (410) → token removed, multi-device fan-out, and off-thread (non-blocking)
dispatch.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import Executor
from typing import Any, List, Optional

import pytest

from hermes_push.sender import (
    GatewaySender,
    HttpResponse,
    _register_url_for,
)
from hermes_push.store import TokenStore


PUSH_URL = "https://gw.example/push"
REGISTER_URL = "https://gw.example/register"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeHttp:
    """Routes POSTs by URL, returning scripted per-URL responses (or raising).

    A ``register_responses`` / ``push_responses`` list is consumed in order for
    the matching URL; when a list is exhausted a 200 (push) / a default
    capability (register) is returned. This lets a test distinguish the
    ``/register`` call from the ``/push`` call.
    """

    def __init__(
        self,
        *,
        register_responses: Optional[List[Any]] = None,
        push_responses: Optional[List[Any]] = None,
        default_capability: str = "cap-default",
    ) -> None:
        self._register = list(register_responses or [])
        self._push = list(push_responses or [])
        self._default_capability = default_capability
        self.register_calls: List[dict] = []
        self.push_calls: List[dict] = []

    def post_json(self, url: str, body: bytes, *, timeout: float) -> HttpResponse:
        parsed = json.loads(body.decode("utf-8"))
        call = {"url": url, "body": parsed, "timeout": timeout}
        if url == REGISTER_URL:
            self.register_calls.append(call)
            item = self._register.pop(0) if self._register else HttpResponse(
                status=200, body=json.dumps({"capability": self._default_capability})
            )
        else:
            self.push_calls.append(call)
            item = self._push.pop(0) if self._push else HttpResponse(status=200, body="")
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
        gateway_url=PUSH_URL,
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
# Register-URL derivation
# ---------------------------------------------------------------------------


def test_register_url_replaces_push_suffix():
    assert _register_url_for("https://gw.example/push") == "https://gw.example/register"
    assert (
        _register_url_for("https://gw.example/api/push")
        == "https://gw.example/api/register"
    )


def test_register_url_falls_back_to_origin_when_no_push_suffix():
    assert _register_url_for("https://gw.example/notify") == "https://gw.example/register"
    assert _register_url_for("https://gw.example") == "https://gw.example/register"


# ---------------------------------------------------------------------------
# Capability fetch + payload shape on the wire
# ---------------------------------------------------------------------------


def test_first_push_registers_for_capability_then_pushes_with_it(tmp_path):
    store = _store(tmp_path)
    store.upsert(device_token="dt-1", apns_env="production", app_version="1.0")
    http = FakeHttp(
        register_responses=[
            HttpResponse(status=200, body=json.dumps({"capability": "cap-xyz"}))
        ],
        push_responses=[HttpResponse(status=200, body='{"ok":true}')],
    )
    sender = _make_sender(store, http)

    sender.send(GENERIC_PAYLOAD)

    # Registered for a capability, then pushed once.
    assert len(http.register_calls) == 1
    assert http.register_calls[0]["url"] == REGISTER_URL
    assert http.register_calls[0]["body"] == {"device_token": "dt-1"}

    assert len(http.push_calls) == 1
    sent = http.push_calls[0]["body"]
    assert sent["device_token"] == "dt-1"
    assert sent["apns_env"] == "production"
    assert sent["type"] == "complete"
    assert sent["session_id"] == "sess-1"
    assert sent["title"] == "Turn complete"
    assert sent["body"] == "Hermes finished the turn"
    assert sent["thread_id"] == "sess-1"
    # The capability is presented; NO hmac is ever sent.
    assert sent["capability"] == "cap-xyz"
    assert "hmac" not in sent
    assert http.push_calls[0]["url"] == PUSH_URL
    assert http.push_calls[0]["timeout"] == pytest.approx(5.0)

    # Capability is cached on the device record.
    assert store.get("dt-1")["capability"] == "cap-xyz"


def test_subsequent_push_reuses_cached_capability(tmp_path):
    store = _store(tmp_path)
    store.upsert(device_token="dt-1", apns_env="production", app_version="1.0")
    store.set_capability("dt-1", "cap-cached")
    http = FakeHttp()  # no register response scripted; should not be needed
    sender = _make_sender(store, http)

    sender.send(GENERIC_PAYLOAD)

    # No second registration — the stored capability is reused.
    assert http.register_calls == []
    assert len(http.push_calls) == 1
    assert http.push_calls[0]["body"]["capability"] == "cap-cached"


def test_send_omits_thread_id_when_absent(tmp_path):
    store = _store(tmp_path)
    store.upsert(device_token="dt-1", apns_env="sandbox", app_version="1.0")
    store.set_capability("dt-1", "cap-1")
    http = FakeHttp()
    sender = _make_sender(store, http)

    payload = {
        "type": "approval",
        "session_id": "s1",
        "title": "Approval needed",
        "body": "Hermes needs your approval",
    }
    sender.send(payload)

    sent = http.push_calls[0]["body"]
    assert "thread_id" not in sent
    assert "hmac" not in sent
    assert sent["capability"] == "cap-1"


def test_no_devices_means_no_post(tmp_path):
    store = _store(tmp_path)  # empty
    http = FakeHttp()
    sender = _make_sender(store, http)
    sender.send(GENERIC_PAYLOAD)
    assert http.register_calls == []
    assert http.push_calls == []


# ---------------------------------------------------------------------------
# Register failure → device skipped (no crash, no push)
# ---------------------------------------------------------------------------


def test_register_non_2xx_skips_device(tmp_path):
    store = _store(tmp_path)
    store.upsert(device_token="dt-1", apns_env="production", app_version="1.0")
    http = FakeHttp(register_responses=[HttpResponse(status=500, body="boom")])
    sender = _make_sender(store, http)

    sender.send(GENERIC_PAYLOAD)

    assert len(http.register_calls) == 1
    assert http.push_calls == []  # skipped this round
    assert "capability" not in store.get("dt-1")


def test_register_network_error_skips_device(tmp_path):
    store = _store(tmp_path)
    store.upsert(device_token="dt-1", apns_env="production", app_version="1.0")
    http = FakeHttp(register_responses=[ConnectionError("down")])
    sender = _make_sender(store, http)

    sender.send(GENERIC_PAYLOAD)

    assert len(http.register_calls) == 1
    assert http.push_calls == []


def test_register_malformed_body_skips_device(tmp_path):
    store = _store(tmp_path)
    store.upsert(device_token="dt-1", apns_env="production", app_version="1.0")
    http = FakeHttp(register_responses=[HttpResponse(status=200, body="not json")])
    sender = _make_sender(store, http)

    sender.send(GENERIC_PAYLOAD)

    assert http.push_calls == []


# ---------------------------------------------------------------------------
# 403 → clear capability, re-register once, retry push once
# ---------------------------------------------------------------------------


def test_403_refreshes_capability_once_then_retries(tmp_path):
    store = _store(tmp_path)
    store.upsert(device_token="dt-1", apns_env="production", app_version="1.0")
    store.set_capability("dt-1", "cap-stale")
    http = FakeHttp(
        register_responses=[
            HttpResponse(status=200, body=json.dumps({"capability": "cap-fresh"}))
        ],
        push_responses=[
            HttpResponse(status=403, body='{"error":"invalid_capability"}'),
            HttpResponse(status=200, body='{"ok":true}'),
        ],
    )
    sender = _make_sender(store, http)

    sender.send(GENERIC_PAYLOAD)

    # Exactly one refresh (one /register), two pushes (stale then fresh).
    assert len(http.register_calls) == 1
    assert len(http.push_calls) == 2
    assert http.push_calls[0]["body"]["capability"] == "cap-stale"
    assert http.push_calls[1]["body"]["capability"] == "cap-fresh"
    # The fresh capability is persisted.
    assert store.get("dt-1")["capability"] == "cap-fresh"


def test_403_gives_up_after_single_refresh(tmp_path):
    store = _store(tmp_path)
    store.upsert(device_token="dt-1", apns_env="production", app_version="1.0")
    store.set_capability("dt-1", "cap-stale")
    http = FakeHttp(
        register_responses=[
            HttpResponse(status=200, body=json.dumps({"capability": "cap-fresh"}))
        ],
        # Both pushes (stale + fresh) are rejected → give up, no unbounded loop.
        push_responses=[
            HttpResponse(status=403, body='{"error":"invalid_capability"}'),
            HttpResponse(status=403, body='{"error":"invalid_capability"}'),
        ],
    )
    sender = _make_sender(store, http, max_attempts=3)

    sender.send(GENERIC_PAYLOAD)

    # Exactly one refresh and exactly two pushes — no third re-register/retry.
    assert len(http.register_calls) == 1
    assert len(http.push_calls) == 2


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------


def test_multi_device_fanout_each_with_own_capability(tmp_path):
    store = _store(tmp_path)
    store.upsert(device_token="dt-A", apns_env="production", app_version="1")
    store.upsert(device_token="dt-B", apns_env="sandbox", app_version="1")
    store.set_capability("dt-A", "cap-A")
    store.set_capability("dt-B", "cap-B")
    http = FakeHttp(
        push_responses=[HttpResponse(status=200, body=""), HttpResponse(status=200, body="")]
    )
    sender = _make_sender(store, http)

    sender.send(GENERIC_PAYLOAD)

    assert http.register_calls == []  # both cached
    assert len(http.push_calls) == 2
    by_token = {c["body"]["device_token"]: c["body"] for c in http.push_calls}
    assert set(by_token) == {"dt-A", "dt-B"}
    assert by_token["dt-A"]["apns_env"] == "production"
    assert by_token["dt-B"]["apns_env"] == "sandbox"
    assert by_token["dt-A"]["capability"] == "cap-A"
    assert by_token["dt-B"]["capability"] == "cap-B"
    assert "hmac" not in by_token["dt-A"]


def test_one_device_failure_does_not_block_others(tmp_path):
    store = _store(tmp_path)
    store.upsert(device_token="dt-A", apns_env="production", app_version="1")
    store.upsert(device_token="dt-B", apns_env="production", app_version="1")
    store.set_capability("dt-A", "cap-A")
    store.set_capability("dt-B", "cap-B")
    # First device: persistent transport error (exhausts retries). Second: ok.
    http = FakeHttp(
        push_responses=[
            ConnectionError("boom"),
            ConnectionError("boom"),
            ConnectionError("boom"),
            HttpResponse(status=200, body=""),
        ]
    )
    sender = _make_sender(store, http, max_attempts=3)

    sender.send(GENERIC_PAYLOAD)

    # 3 failed attempts for the first device, then 1 success for the second.
    assert len(http.push_calls) == 4
    assert http.push_calls[-1]["body"]["device_token"] in {"dt-A", "dt-B"}


# ---------------------------------------------------------------------------
# Timeout / retry
# ---------------------------------------------------------------------------


def test_transient_failure_then_success_retries(tmp_path):
    store = _store(tmp_path)
    store.upsert(device_token="dt", apns_env="production", app_version="1")
    store.set_capability("dt", "cap")
    http = FakeHttp(push_responses=[TimeoutError("slow"), HttpResponse(status=200, body="")])
    sender = _make_sender(store, http, max_attempts=3)

    sender.send(GENERIC_PAYLOAD)

    assert len(http.push_calls) == 2  # one retry, then success


def test_gives_up_after_max_attempts(tmp_path):
    store = _store(tmp_path)
    store.upsert(device_token="dt", apns_env="production", app_version="1")
    store.set_capability("dt", "cap")
    http = FakeHttp(
        push_responses=[TimeoutError("x"), TimeoutError("x"), TimeoutError("x")]
    )
    sender = _make_sender(store, http, max_attempts=3)

    sender.send(GENERIC_PAYLOAD)

    assert len(http.push_calls) == 3  # exactly max_attempts, then give up


def test_5xx_is_retried_4xx_is_not(tmp_path):
    store = _store(tmp_path)
    store.upsert(device_token="dt", apns_env="production", app_version="1")
    store.set_capability("dt", "cap")
    # 503 (retry) then 400 (give up immediately).
    http = FakeHttp(
        push_responses=[HttpResponse(status=503, body=""), HttpResponse(status=400, body="bad")]
    )
    sender = _make_sender(store, http, max_attempts=5)

    sender.send(GENERIC_PAYLOAD)

    assert len(http.push_calls) == 2  # 503 retried once, 400 gives up


# ---------------------------------------------------------------------------
# Prune (HTTP 410 — the single prune signal)
# ---------------------------------------------------------------------------


def test_410_prunes_token(tmp_path):
    store = _store(tmp_path)
    store.upsert(device_token="dead", apns_env="production", app_version="1")
    store.set_capability("dead", "cap")
    http = FakeHttp(push_responses=[HttpResponse(status=410, body='{"device_token":"dead"}')])
    sender = _make_sender(store, http)

    sender.send(GENERIC_PAYLOAD)

    assert store.get("dead") is None
    assert len(http.push_calls) == 1  # no retry after a prune


def test_non_410_status_does_not_prune(tmp_path):
    # 410 is the ONLY prune signal — a body alone on a non-410 status must NOT prune.
    store = _store(tmp_path)
    store.upsert(device_token="alive", apns_env="production", app_version="1")
    store.set_capability("alive", "cap")
    http = FakeHttp(push_responses=[HttpResponse(status=200, body='{"device_token":"alive"}')])
    sender = _make_sender(store, http)

    sender.send(GENERIC_PAYLOAD)

    assert store.get("alive") is not None


def test_success_does_not_prune(tmp_path):
    store = _store(tmp_path)
    store.upsert(device_token="live", apns_env="production", app_version="1")
    store.set_capability("live", "cap")
    http = FakeHttp(push_responses=[HttpResponse(status=200, body='{"ok":true}')])
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
    store.set_capability("dt", "cap")
    http = FakeHttp()
    rec = _RecordingExecutor()
    sender = GatewaySender(
        store=store, gateway_url=PUSH_URL, http_client=http, executor=rec
    )

    sender.send(GENERIC_PAYLOAD)

    # Work was handed to the executor, NOT run inline (no HTTP call yet).
    assert len(rec.submitted) == 1
    assert http.push_calls == []
    # Running the captured work performs the POST.
    fn, args, kwargs = rec.submitted[0]
    fn(*args, **kwargs)
    assert len(http.push_calls) == 1


def test_send_returns_quickly_with_slow_http(tmp_path):
    """A slow gateway must not block the calling (hook/turn) thread."""
    store = _store(tmp_path)
    store.upsert(device_token="dt", apns_env="production", app_version="1")
    store.set_capability("dt", "cap")  # cached so no register round-trip

    started = threading.Event()
    release = threading.Event()

    class SlowHttp:
        def post_json(self, url, body, *, timeout):
            started.set()
            release.wait(timeout=5)
            return HttpResponse(status=200, body="")

    # Real (default) thread-pool executor here so delivery is genuinely off-thread.
    sender = GatewaySender(
        store=store, gateway_url=PUSH_URL, http_client=SlowHttp()
    )

    t0 = time.monotonic()
    sender.send(GENERIC_PAYLOAD)
    elapsed = time.monotonic() - t0

    # send() returned essentially immediately despite the blocking POST.
    assert elapsed < 0.5
    assert started.wait(timeout=2), "delivery never started on the background thread"
    release.set()
    sender.shutdown(wait=True)
