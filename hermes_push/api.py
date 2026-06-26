"""hermes-push REST router (Task B2).

Mounted by the Hermes dashboard plugin system at ``/api/plugins/hermes-push/``
(see ``dashboard/manifest.json`` ``api`` field, which re-exports this ``router``
via ``dashboard/plugin_api.py``).

Routes
------
* ``POST /register``   ``{device_token, apns_env, app_version}`` → upsert into
  the token store. The app holds no secret and does not sign pushes; the gateway
  issues a device-scoped capability that the sender fetches and caches (see
  ``sender.py``), so no secret is minted or returned here.
* ``POST /unregister`` ``{device_token}`` → remove the token from the store.

Auth (host behaviour, confirmed against hermes-agent in B1 + re-verified in B2)
------------------------------------------------------------------------------
All ``/api/*`` routes — including ``/api/plugins/*`` — are gated by the
dashboard's session-token auth middleware
(``hermes_cli/web_server.py::auth_middleware``, mirrored by
``dashboard_auth.middleware.gated_auth_middleware`` in the OAuth-gated regime).
The only exemptions are the small read-only allowlist in
``hermes_cli/dashboard_auth/public_paths.py`` (``/api/status``,
``/api/dashboard/plugins``, …) — our ``/register`` / ``/unregister`` routes are
NOT in it, so they require ``X-Hermes-Session-Token`` (or the session cookie).
We therefore do NOT re-implement auth here; doing so would double-gate and
break the token-mode byte-compat path. (If a future host change moves plugin
routes out from under that middleware, enforce the header in these handlers.)

Push authorization
------------------
The plugin holds **no shared secret**. The gateway issues a device-scoped
**capability** that the sender fetches and presents on each push (see
``sender.py``). The app never signs, so registration neither mints nor returns
any secret.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from hermes_push.store import TokenStore
from hermes_push.triggers import make_payload

# The web server imports this module and looks for a module-level ``router``
# attribute (hermes_cli/web_server.py::_mount_plugin_api_routes).
router = APIRouter()

# Module-level store, lazily created so tests can swap in a tmp-dir store via
# :func:`set_store` before issuing requests. Kept module-level (not a FastAPI
# dependency) to mirror the simple plugin convention and keep the host wiring
# zero-config.
_store: Optional[TokenStore] = None


def set_store(store: TokenStore) -> None:
    """Override the module store (used by tests to point at a tmp dir)."""
    global _store
    _store = store


def get_store() -> TokenStore:
    """Return the active store, creating the default disk-backed one lazily."""
    global _store
    if _store is None:
        _store = TokenStore()
    return _store


# The GatewaySender used by the ``/test`` route. Wired by ``__init__._wire_pipeline``
# (so a test push reuses the very same outbound pipeline as real triggers) and swappable
# by tests via :func:`set_sender`. ``None`` until the plugin is registered.
_sender: Optional[Any] = None


def set_sender(sender: Any) -> None:
    """Override the module sender (used by the plugin wiring + tests)."""
    global _sender
    _sender = sender


def get_sender() -> Any:
    """Return the active sender, or ``None`` if the pipeline hasn't been wired yet."""
    return _sender


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    device_token: str = Field(..., min_length=1)
    apns_env: str = Field(..., pattern="^(sandbox|production)$")
    app_version: str = Field(..., min_length=1)


class RegisterResponse(BaseModel):
    ok: bool = True
    device_token: str
    apns_env: str


class UnregisterRequest(BaseModel):
    device_token: str = Field(..., min_length=1)


class UnregisterResponse(BaseModel):
    ok: bool = True
    removed: bool


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/register", response_model=RegisterResponse)
def register(req: RegisterRequest) -> RegisterResponse:
    """Register (or refresh) an APNs device token.

    No secret is minted or returned — the gateway issues a device-scoped
    capability that the sender fetches and caches (see ``sender.py``), so the app
    stores nothing beyond the device token it already has.
    """
    store = get_store()
    record = store.upsert(
        device_token=req.device_token,
        apns_env=req.apns_env,
        app_version=req.app_version,
    )
    return RegisterResponse(
        device_token=record["device_token"],
        apns_env=record["apns_env"],
    )


@router.post("/unregister", response_model=UnregisterResponse)
def unregister(req: UnregisterRequest) -> UnregisterResponse:
    """Remove a device token from the store."""
    store = get_store()
    removed = store.remove(req.device_token)
    return UnregisterResponse(removed=removed)


class TestPushResponse(BaseModel):
    ok: bool = True
    # How many registered devices the sample push was fanned out to.
    devices: int


@router.post("/test", response_model=TestPushResponse)
def send_test(req: Optional[dict] = None) -> TestPushResponse:
    """Deliver a sample push to this caller's registered device(s).

    Backs the iOS Settings "Send test notification" button (C6): looks up the
    registered devices and fans a generic ``complete``-type sample payload out
    through the same :class:`GatewaySender` pipeline real triggers use (gateway
    capability, off-thread POST, 410→prune). It intentionally **bypasses the
    suppression policy** — a test push is an explicit user action that should
    always go out, regardless of live-client / duration / dedup gates.

    Honors the no-content privacy rule: the payload carries only a generic
    title/body + a synthetic ``session_id`` (see :func:`make_payload` /
    ``triggers.TITLES``). Returns 404 if the pipeline isn't wired (mirrors the
    "plugin not installed" capability-gate the app expects).
    """
    sender = get_sender()
    if sender is None:
        raise HTTPException(status_code=404, detail="push pipeline not initialized")
    store = get_store()
    devices = len(store.list_all())
    # Synthetic session id so the test push collapses on its own thread and never
    # spoofs a real session. Generic "complete" body — no content leaks.
    payload = make_payload("complete", f"test-{uuid.uuid4().hex[:8]}")
    sender.send(payload)
    return TestPushResponse(devices=devices)
