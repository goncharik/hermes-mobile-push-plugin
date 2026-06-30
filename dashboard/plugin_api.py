"""Dashboard backend entry for hermes-push.

The Hermes web server imports this file STANDALONE by path and reads its
module-level ``router`` attribute
(``hermes_cli/web_server.py::_mount_plugin_api_routes``). The importer:

* validates the manifest ``api`` path stays *inside* the plugin's ``dashboard/``
  directory (so the file must live here, not one level up), and
* loads it via ``spec_from_file_location`` under the synthetic module name
  ``hermes_dashboard_plugin_hermes-push`` — it is NOT part of the plugin
  package. So this file CANNOT ``from .api import router`` (no package) and
  CANNOT ``from hermes_push.api import router`` (there is no top-level
  ``hermes_push`` on ``sys.path`` when the plugin is installed as a directory —
  the hook side loads it as ``hermes_plugins.hermes_push``).

Pattern (mirrors ``plugins/kanban/dashboard/plugin_api.py`` in hermes-agent:
a self-contained standalone router that reaches shared state without importing
the plugin's own package). We reach the plugin's logic by loading its sibling
modules (``store.py`` / ``triggers.py`` / ``sender.py``, one directory up) by
FILE PATH under a private synthetic package, so their inter-module relative
imports (``from .store import TokenStore``) still resolve. The on-disk token
store (``$HERMES_HOME/hermes-push/tokens.json``) is the shared-state seam: the
routes here and the hooks loaded as ``hermes_plugins.hermes_push`` both read and
write the SAME file, so a ``GatewaySender`` constructed here delivers to exactly
the devices the hooks see. No in-process object needs to be shared across the
two loaders.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Load the plugin's sibling modules by path, under a private synthetic package.
# ---------------------------------------------------------------------------
#
# The plugin root is one directory up from this ``dashboard/`` file. We register
# a package whose ``__path__`` points at that root, then import the submodules
# we need into it. Registering the parent package first means the submodules'
# RELATIVE imports (``from .store import TokenStore``) resolve against the same
# synthetic package rather than failing with "attempted relative import with no
# known parent package".
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_PKG_NAME = "_hermes_push_dashboard_pkg"


def _load_plugin_module(name: str) -> types.ModuleType:
    """Import ``<plugin_root>/<name>.py`` as ``_PKG_NAME.<name>`` (cached)."""
    if _PKG_NAME not in sys.modules:
        pkg = types.ModuleType(_PKG_NAME)
        pkg.__path__ = [str(_PLUGIN_ROOT)]  # type: ignore[attr-defined]
        pkg.__package__ = _PKG_NAME
        sys.modules[_PKG_NAME] = pkg

    full_name = f"{_PKG_NAME}.{name}"
    cached = sys.modules.get(full_name)
    if cached is not None:
        return cached

    spec = importlib.util.spec_from_file_location(
        full_name, _PLUGIN_ROOT / f"{name}.py"
    )
    if spec is None or spec.loader is None:  # pragma: no cover — packaging error
        raise ImportError(f"hermes-push: cannot load {name}.py from {_PLUGIN_ROOT}")
    module = importlib.util.module_from_spec(spec)
    module.__package__ = _PKG_NAME
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


# triggers has no internal imports; store has none; sender imports `.store`
# (resolved via the synthetic package above).
_triggers = _load_plugin_module("triggers")
_store_mod = _load_plugin_module("store")
_sender_mod = _load_plugin_module("sender")

TokenStore = _store_mod.TokenStore
GatewaySender = _sender_mod.GatewaySender
make_payload = _triggers.make_payload

# The web server looks for this module-level ``router`` attribute.
router = APIRouter()

# Module-level store, lazily created (and swappable by tests via
# :func:`set_store`). Disk-backed, so it shares state with the hook side.
_store: Optional[Any] = None
# Optional sender override (tests). When unset, ``/test`` constructs a fresh
# ``GatewaySender`` over the shared on-disk store.
_sender: Optional[Any] = None


def set_store(store: Any) -> None:
    """Override the module store (used by tests to point at a tmp dir)."""
    global _store
    _store = store


def get_store() -> Any:
    """Return the active store, creating the default disk-backed one lazily."""
    global _store
    if _store is None:
        _store = TokenStore()
    return _store


def set_sender(sender: Any) -> None:
    """Override the module sender (used by tests)."""
    global _sender
    _sender = sender


def get_sender() -> Any:
    """Return the active sender, constructing a default over the shared store.

    Unlike the hook side, the dashboard route runs in a separately-loaded
    module, so it cannot reuse the in-process sender the hooks wired. It builds
    its own ``GatewaySender`` over the SAME on-disk token store — identical
    delivery behaviour (gateway capability, off-thread POST, 410→prune).
    """
    global _sender
    if _sender is None:
        _sender = GatewaySender(store=get_store())
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


class TestPushResponse(BaseModel):
    ok: bool = True
    # How many registered devices the sample push was fanned out to.
    devices: int


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


@router.post("/test", response_model=TestPushResponse)
def send_test(req: Optional[dict] = None) -> TestPushResponse:
    """Deliver a sample push to this caller's registered device(s).

    Backs the iOS Settings "Send test notification" button: looks up the
    registered devices and fans a generic ``complete``-type sample payload out
    through a :class:`GatewaySender` over the shared on-disk store (gateway
    capability, off-thread POST, 410→prune). It intentionally **bypasses the
    suppression policy** — a test push is an explicit user action that should
    always go out.

    Honors the no-content privacy rule: the payload carries only a generic
    title/body + a synthetic ``session_id`` (see :func:`make_payload`).
    """
    store = get_store()
    sender = get_sender()
    devices = len(store.list_all())
    payload = make_payload("complete", f"test-{uuid.uuid4().hex[:8]}")
    sender.send(payload)
    return TestPushResponse(devices=devices)
