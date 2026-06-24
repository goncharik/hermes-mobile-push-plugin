"""Tests for hermes-push plugin registration wiring (Task B1).

Standalone: these mock the Hermes ``PluginContext`` so the suite runs without
hermes-agent installed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import hermes_push


# Hooks this plugin must wire in, matching hermes-agent's VALID_HOOKS.
EXPECTED_HOOKS = {
    "pre_approval_request",
    "pre_tool_call",
    "pre_llm_call",
    "post_llm_call",
    "on_session_end",
}


class FakeCtx:
    """Minimal stand-in for hermes-agent's PluginContext.

    Records hook registrations the way the real manager does
    (hermes_cli/plugins.py::PluginContext.register_hook → manager._hooks).
    """

    def __init__(self) -> None:
        self.hooks: dict[str, list] = {}

    def register_hook(self, hook_name: str, callback) -> None:
        assert callable(callback), f"hook {hook_name!r} handler is not callable"
        self.hooks.setdefault(hook_name, []).append(callback)


def test_register_wires_expected_hooks():
    ctx = FakeCtx()
    hermes_push.register(ctx)

    assert set(ctx.hooks) == EXPECTED_HOOKS
    # Exactly one handler per hook — no double-registration.
    for name, callbacks in ctx.hooks.items():
        assert len(callbacks) == 1, f"hook {name!r} registered {len(callbacks)} times"


def test_register_does_not_raise_with_minimal_ctx():
    # A ctx exposing only register_hook (what B1 uses) must be enough.
    ctx = FakeCtx()
    hermes_push.register(ctx)  # should not raise


def test_hook_handlers_are_noop_safe():
    """Observer handlers must accept arbitrary kwargs and never alter flow.

    Every hook handler returns None (observers); pre_llm_call in particular must
    never return a context dict that would mutate the user message.
    """
    ctx = FakeCtx()
    hermes_push.register(ctx)

    for name, callbacks in ctx.hooks.items():
        handler = callbacks[0]
        # Call with representative-but-unexpected kwargs; must not raise.
        result = handler(
            command="rm -rf /",
            surface="gateway",
            session_id="s1",
            event=object(),
            extra="ignored",
        )
        assert result is None, f"hook {name!r} returned {result!r}; must be None"


def test_rest_router_importable_and_is_apirouter():
    from fastapi import APIRouter

    from hermes_push import api

    assert isinstance(api.router, APIRouter)


def test_dashboard_shim_reexports_router():
    """The dashboard api file the manifest points at must expose ``router``."""
    import importlib.util

    shim_path = Path(__file__).resolve().parent.parent / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_push_dashboard_shim", shim_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    from fastapi import APIRouter

    assert isinstance(module.router, APIRouter)


def test_manifest_api_field_points_at_existing_dashboard_file():
    dashboard_dir = Path(__file__).resolve().parent.parent / "dashboard"
    manifest = json.loads((dashboard_dir / "manifest.json").read_text())

    api_field = manifest.get("api")
    assert api_field, "manifest must declare an 'api' field"
    # Host requires a relative path that stays inside dashboard/.
    assert not Path(api_field).is_absolute()
    assert ".." not in Path(api_field).parts
    assert (dashboard_dir / api_field).exists()


def test_plugin_yaml_name_matches_entry_point():
    import sys

    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover
        pytest.skip("tomllib requires Python 3.11+")

    root = Path(__file__).resolve().parent.parent
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    eps = pyproject["project"]["entry-points"]["hermes_agent.plugins"]

    assert eps.get("hermes-push") == "hermes_push:register"
