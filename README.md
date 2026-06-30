# hermes-push

A **standalone, directory-installed** [Hermes Agent](https://github.com/) plugin
that delivers push notifications to the **Hermes Mobile** iOS app when a turn
finishes or fails, or the agent needs the user — even while the app is
backgrounded and its WebSocket has dropped.

> **Install it as a directory clone** into `~/.hermes/plugins/hermes-push/` (see
> [Install](#install)). That is the only layout the agent loads **both** the
> trigger hooks **and** the `POST /api/plugins/hermes-push/register` route from —
> the agent mounts a plugin's HTTP routes only from a directory plugin's
> `dashboard/manifest.json`, never from a pip-installed package. (`pip install`
> still works, but loads the **hooks only** — no route.)

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

It uses only Hermes Agent's **public plugin API** (the `register(ctx)` hooks and
the dashboard plugin system's `dashboard/manifest.json`). It makes **no changes**
to hermes-agent.

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

### Push authorization

The plugin holds **no shared secret**. Instead the gateway issues a
**device-scoped capability** (an opaque string) that the plugin fetches once per
device, caches in its token store, and presents on every push. On a device's
first push the plugin `POST`s `{"device_token": …}` to the gateway's `/register`
endpoint and stores the returned `{"capability": …}`; subsequent pushes reuse it.
If the gateway rejects a push with `403` (stale/rotated capability) the plugin
drops the cached value, re-registers once, and retries the push. The plugin never
computes the capability itself — it is opaque.

## Install

Install as a **directory plugin** — clone the repo straight into the agent's
user-plugins directory:

```bash
git clone https://github.com/goncharik/hermes-mobile-push-plugin.git \
  ~/.hermes/plugins/hermes-push

# Ensure the one runtime dep is in the agent's Python environment.
# (FastAPI is already present because the agent runs the dashboard; install it
# only if your agent env somehow lacks it.)
pip install fastapi   # usually already satisfied by hermes-agent

# Enable the plugin (opt-in; installing alone does not load it):
hermes plugins enable hermes-push
#   …or add it by hand to ~/.hermes/config.yaml:
#     plugins:
#       enabled:
#         - hermes-push

# Restart the agent.
```

This is the **only** install that loads both halves of the plugin:

- **Trigger hooks** load via the directory loader, which imports
  `~/.hermes/plugins/hermes-push/__init__.py` (its `register(ctx)`).
- **The `POST /api/plugins/hermes-push/register` route** is mounted by the
  agent's dashboard plugin system, which scans
  `~/.hermes/plugins/hermes-push/dashboard/manifest.json` — a **directory-only**
  scan. pip/entry-point packages are never scanned for dashboard routes.

The routes mount under `/api/plugins/hermes-push/` (`/register`, `/unregister`,
`/test`).

> **pip install loads the hooks only.** `pip install hermes-push` (or
> `pip install -e .` from a clone) still works via the `hermes_agent.plugins`
> entry point and registers the trigger hooks, but the agent will **not** mount
> the `/register` route from a pip package — the iOS app would have nowhere to
> register its device token. Use the directory clone above for the full plugin.

Like all non-bundled plugins it is opt-in via `plugins.enabled` in Hermes config.

## Layout

The repo root **is** the `hermes_push` package (flat layout) so the agent's
directory loader can import `__init__.py` directly. Internal imports are
relative.

| Path | Purpose |
|---|---|
| `__init__.py` | `register(ctx)` — wires the trigger hooks (the directory loader's entry) |
| `api.py` | FastAPI `router` (`/register`, `/unregister`, `/test`) — used by the hooks + pip path |
| `policy.py` / `sender.py` / `store.py` / `triggers.py` | suppression policy, gateway sender, token store, trigger→payload mappers |
| `plugin.yaml` | plugin manifest (name, version, hooks) |
| `dashboard/manifest.json` | declares the `api` router file for the dashboard plugin system |
| `dashboard/plugin_api.py` | **self-contained** standalone router (the host imports it by path, outside the package). Loads `store.py`/`triggers.py`/`sender.py` by file path and reaches shared state via the on-disk token store — it does **not** import the `hermes_push` package |
| `tests/` | `pytest` suite (`tests/pytest.ini` pins rootdir to `tests/`) |

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"
pytest tests/          # or: make test
```

Run the suite with **`pytest tests/`** (or `make test`). The repo root is the
`hermes_push` package, so `tests/pytest.ini` pins pytest's rootdir to `tests/`
(otherwise pytest would treat the repo root as a package and fail importing its
`__init__.py` standalone). `tests/conftest.py` registers `hermes_push` the same
way the agent's directory loader does.

Tests mock the Hermes `PluginContext`, so the suite runs standalone without
hermes-agent installed.

## Status

Feature-complete and tested. Implemented: the JSON-file token store, the
trigger→payload mappers (the `pre_approval_request` / `pre_tool_call` /
`post_llm_call` / `on_session_end` hooks), the suppression policy
(client-present / duration / dedup / no-devices gates), and the outbound
`GatewaySender` (off-thread fan-out, bounded retries, gateway-issued device
capability, HTTP-410 prune). Plus a `POST /api/plugins/hermes-push/test` route that fans a
sample push through the real pipeline (backs the iOS Settings "Send test
notification"). All four triggers — approval / turn-complete / error / clarify —
are supported.

## Configuration

| Env var                   | What it is                                                              |
| ------------------------- | ---------------------------------------------------------------------- |
| `HERMES_PUSH_GATEWAY_URL` | Override the push-gateway URL (defaults to the `GATEWAY_URL` const in `sender.py`; set it to your deployed Worker's `…/push` after deploy). The `/register` URL is derived from it (`…/push` → `…/register`). |

The plugin holds **no shared secret** — push authorization uses a device-scoped
capability the gateway issues and the plugin caches (see *Push authorization*
above).

## Routes

- `POST /register` — `{device_token, apns_env, app_version}` → upserts the device
  token. The app never signs, so **no secret is minted or returned**.
- `POST /unregister` — `{device_token}` → removes the token.
- `POST /test` — fans a generic sample push to the registered device(s) through
  the same outbound pipeline real triggers use (bypasses suppression).
