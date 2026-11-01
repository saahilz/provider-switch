# Claude Code Provider Switcher — Design

Date: 2026-10-24
Target: Claude Code 2.1.306 (verified against official docs + installed CLI)

## Verified platform facts

| Fact | Source |
|---|---|
| Plugins ship skills, agents, hooks, MCP servers, LSP, monitors, `bin/`. Plugin `settings` field applies **only** `agent` and `subagentStatusLine`. | plugin manifest reference |
| `${CLAUDE_PLUGIN_ROOT}` resolves inside skill/command Markdown bodies; **not** in `apiKeyHelper`. | manifest reference, "Where each variable resolves" |
| `env` in settings applies at startup **and again mid-session when the merged `env` changes**. | settings reference, "When Claude Code applies env values" |
| `apiKeyHelper` runs through `/bin/sh` (POSIX) or `cmd` (Windows), output sent as **both** `X-Api-Key` and `Authorization: Bearer`. Cached 5 min (`CLAUDE_CODE_API_KEY_HELPER_TTL_MS`), re-run on 401/403. | settings reference, apiKeyHelper |
| Helper re-run on 401/403 only applies when **`ANTHROPIC_AUTH_TOKEN` is not set**. | settings reference, apiKeyHelper |
| `model` is read once at session start; mid-session changes need `/model`. A top-level `model` setting outranks `ANTHROPIC_MODEL`. | settings reference |
| Hooks **cannot** rewrite settings, env, model, or provider. `PreModelSwitch`/`ConfigChange` can only deny. | hooks reference |
| `userConfig` with `sensitive: true` stores to the OS credential store, but is exposed only to **hook** processes (`CLAUDE_PLUGIN_OPTION_*`) and as a placeholder in skill content — not readable by `apiKeyHelper`. | manifest reference |

Consequence: the only supported, secret-free way to supply a credential is `apiKeyHelper`.

## Architecture

```
/provider-switch switch <name>
        │
        ▼  (Claude runs, via Bash tool)
 scripts/provider_switch.py
        ├── profiles.json        metadata only, no secrets
        ├── credential store     OS store; secret never touches disk in plaintext
        └── settings.json        writes ONLY managed keys, atomic, backed up
                    │
                    ▼  Claude Code reloads settings mid-session
             apiKeyHelper → scripts/api_key_helper.py
                    │  reads active profile → OS credential store
                    └─ prints secret on stdout (memory only)
```

Three moving parts, each independently testable:

1. **`config.py`** — profile store (JSON, 0600). Knows providers, not secrets.
2. **`settings_editor.py`** — atomic edit of a Claude Code settings file, touching only managed keys, with backup + transaction record.
3. **`credential_store.py`** — OS credential store abstraction; refuses rather than falling back to plaintext.

The switcher's core logic is provider-agnostic: every provider reduces to *base URL + auth style + model*, because Claude Code speaks one wire protocol. Adding a provider is a registry entry, not a code change.

## Managed settings keys

Only these are ever written, and every one is restored by rollback:

- `apiKeyHelper` → absolute path to `api_key_helper.py`
- `env.ANTHROPIC_BASE_URL`
- `env.ANTHROPIC_MODEL`
- `env.ANTHROPIC_API_KEY` → `""` (cancel a stale key so the helper wins)
- `env.ANTHROPIC_AUTH_TOKEN` → `""` (documented: helper is ignored while this is set)
- `env` keys declared by the provider (`CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, …)

Profiles with `auth: "none"` (Bedrock/Vertex) remove `apiKeyHelper`
instead of pointing it at a key that does not exist; they authenticate
through the cloud provider's own credential chain.

Everything else in the file is preserved byte-for-byte in intent (the file is re-serialized, so formatting is normalised to 2-space JSON, values untouched).

## When a switch takes effect

Verified from the settings reference, not assumed:

| Change | Timing |
|---|---|
| `env` (base URL, model, credential cancellations) | **Next request.** Claude Code re-applies the merged `env` when a saved change alters it. |
| `apiKeyHelper` | **Next helper refresh** — 5 min default, or on a 401/403 while `ANTHROPIC_AUTH_TOKEN` is unset (this switcher keeps it unset). Restart Claude Code to force a re-read. |
| Model in the running conversation | `ANTHROPIC_MODEL` applies to new requests, **unless** a top-level `model` setting or a `/model` choice is in force; `switch` warns when it sees one. Already-cached prompt prefixes are re-read uncached on model change. |

The switch command prints this timing every time. It does not claim "instant".

## Security decisions

- **No plaintext fallback.** If no OS credential store is usable, credential writes fail with remediation steps. Never writes a key to `profiles.json`, settings, or a temp file.
- **Interactive entry only.** `getpass` at a hidden prompt (or one line on stdin when nothing is attached to a terminal) — never argv, never shell history, never echoed. The secret reaches the OS tool on stdin.
- **Reference indirection.** `profiles.json` holds `credential: "<ref>"` only.
- **Redaction.** One `redact()` used by every error path and diagnostic. Tests assert a key never appears.
- **Custom host warning.** Any base URL whose host is not the provider's known host requires explicit confirmation or `--yes`.
- **No credential forwarding on redirect.** `test` refuses HTTP redirects, so a hostile endpoint cannot bounce the key elsewhere.
- **Deletion.** Removes the OS store entry; documents that OS-level backup/journal copies are outside our control.

## Provider extension path

`~/.claude/plugins/data/provider-switch/providers.json` — a data file, not code.
It is merged over the bundled `scripts/providers.json`, replacing entries by
`id` (installed plugins live in a cache copy, so users extend here rather
than editing the bundled file):

```json
{
  "providers": [{
    "id": "my-gateway",
    "label": "My Gateway",
    "auth": "bearer_token",
    "defaultBaseUrl": "https://gw.example.com",
    "models": ["..."],
    "requiresBaseUrl": true,
    "knownHosts": ["gw.example.com"],
    "env": {},
    "capabilities": { "modelDiscovery": false, "testConnection": true }
  }]
}
```

Write the file, run `provider_switch.py validate`, done. No core code changes.

## Human-factors choice (my call)

Switching prints a one-line summary and the effect timing, and does not open a dialog. `remove` and `credential delete` confirm explicitly; `switch` confirms only when the target uses a custom host. `rollback` restores only the managed keys captured in the transaction, so it does not need a confirmation and cannot revert unrelated settings.
