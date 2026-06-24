# hermes-push

A **standalone, pip-installable** [Hermes Agent](https://github.com/) plugin that
delivers push notifications to the **Hermes Mobile** iOS app when a turn finishes
or fails, or the agent needs the user — even while the app is backgrounded and
its WebSocket has dropped.

## Triggers

All triggers use **real plugin hooks** (`hermes_cli/plugins.py::VALID_HOOKS`),
so they fire in both CLI and gateway sessions:

| Push type  | Hook                   | When |
|------------|------------------------|------|
| `approval` | `pre_approval_request` | A dangerous tool needs approval (gateway surface only; CLI prompts are skipped). |
| `complete` | `post_llm_call`        | A user turn finished successfully (after the tool-loop, once a final assistant response exists). Gated to turns longer than ~10s + deduped. |
| `error`    | `on_session_end`       | A turn ended in a genuine failure (`completed is False and interrupted is False`). User interrupts and successes never push. |
| `clarify`  | `pre_tool_call`        | The model calls the `clarify` tool (input needed), **before** the user is prompted. `pre_tool_call` fires for every tool, so we filter to `tool_name == "clarify"`; like approval it is an attention-needed pause and is **not** duration-gated. We are an observer and never block the tool. |

The turn-complete **duration gate** is anchored by the `pre_llm_call` hook
(turn start) and cleared at `on_session_end`. `pre_llm_call`, `post_llm_call` and
`on_session_end` all carry `session_id == agent.session_id`, so they line up with
each other and with the approval hook's `session_key` and `pre_tool_call`'s
`session_id`.

> **Privacy:** the hooks pass message content (`user_message`,
> `assistant_response`, `conversation_history`) and tool args (the clarify
> `question` / `choices`) — the plugin reads ONLY `session_id` from them. No
> content ever leaves the plugin.

It uses only Hermes Agent's **public plugin API** (entry-point group
`hermes_agent.plugins`, `register(ctx)`). It makes **no changes** to
hermes-agent.

> This directory is kept **local** to the `hermes-mobile` repo (gitignored) and
> is **not** pushed. It is published separately (PyPI / install-from-source) so
> self-hosters can `pip install hermes-push` and restart their agent.

## Architecture

```
┌─ User's machine ────────────┐      ┌─ Publisher ────┐      ┌─ Apple ─┐     ┌ Phone ┐
│  Hermes Agent               │      │ Push gateway   │      │  APNs   │     │  App  │
│   └─ hermes-push (plugin)   │ ───► │ (serverless fn)│ ───► │         │ ──► │       │
│      hooks + REST route     │ POST │ holds .p8/JWT  │ HTTP │         │     │       │
└─────────────────────────────┘      └────────────────┘      └─────────┘     └───────┘
        ▲ device token + apns_env registered by app over Tailscale ─────────────┘
```

- **Triggers** run on the user's agent (this plugin); it holds no Apple secret.
- The iOS app registers its APNs device token with the agent via this plugin's
  REST route (`POST /api/plugins/hermes-push/register`).
- On a trigger, the plugin POSTs a **generic** `{type, session_id, title, body}`
  (no message content) to the publisher's stateless push **gateway**, the only
  place the APNs `.p8` key lives. The gateway forwards to APNs.

Privacy: only a generic title/body + `session_id` transit the gateway; real
message content is fetched in-app over the user's private network.

## Install

```bash
pip install hermes-push        # or: pip install -e .  from this directory
hermes plugins enable hermes-push
# restart the agent
```

The plugin is discovered via its `hermes_agent.plugins` entry point. Like all
non-bundled plugins it is opt-in via `plugins.enabled` in Hermes config.

## Layout

| Path | Purpose |
|---|---|
| `hermes_push/__init__.py` | `register(ctx)` — wires trigger hooks |
| `hermes_push/api.py` | FastAPI `router` (`/register`, `/unregister`) |
| `plugin.yaml` | plugin manifest (name, version, hooks) |
| `dashboard/manifest.json` | declares the `api` router file for the dashboard plugin system |
| `dashboard/plugin_api.py` | thin shim re-exporting `hermes_push.api.router` (the host imports the `api` file from inside `dashboard/`) |
| `tests/` | `pytest` suite |

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"
python -m pytest
```

Tests mock the Hermes `PluginContext`, so the suite runs standalone without
hermes-agent installed.

## Status

Feature-complete and tested. Implemented: the JSON-file token store, the
trigger→payload mappers (the `pre_approval_request` / `pre_tool_call` /
`post_llm_call` / `on_session_end` hooks), the suppression policy
(client-present / duration / dedup / no-devices gates), and the outbound
`GatewaySender` (off-thread fan-out, bounded retries, shared-secret HMAC,
HTTP-410 prune). Plus a `POST /api/plugins/hermes-push/test` route that fans a
sample push through the real pipeline (backs the iOS Settings "Send test
notification"). All four triggers — approval / turn-complete / error / clarify —
are supported.

## Configuration

| Env var                   | What it is                                                              |
| ------------------------- | ---------------------------------------------------------------------- |
| `HERMES_PUSH_GATEWAY_URL` | Override the push-gateway URL (defaults to the `GATEWAY_URL` const in `sender.py`; set it to your deployed Worker's `…/push` after deploy). |
| `HERMES_PUSH_HMAC_SECRET` | **Shared** HMAC secret. Provision the SAME value to the gateway (its `HMAC_SECRET`). The plugin signs every push with it; if unset, pushes are sent **unsigned** (the gateway allows unsigned) and a one-time warning is logged. |

## Routes

- `POST /register` — `{device_token, apns_env, app_version}` → upserts the device
  token. The app never signs, so **no secret is minted or returned**.
- `POST /unregister` — `{device_token}` → removes the token.
- `POST /test` — fans a generic sample push to the registered device(s) through
  the same outbound pipeline real triggers use (bypasses suppression).
