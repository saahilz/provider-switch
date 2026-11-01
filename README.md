# Provider Switch

Switch Claude Code between AI provider profiles — Anthropic, an
Anthropic-compatible gateway, Amazon Bedrock, Google Vertex AI, or any
endpoint you describe — without editing settings by hand. Each profile
carries a provider, endpoint, model and a credential *reference*. API keys
are stored in the operating system's credential store and never appear in
settings files, profiles, command lines, logs, or this tool's output.

## Requirements

- Claude Code with plugin support, `apiKeyHelper` and the `env` settings
  block. Built and validated against **Claude Code 2.1.306** on Windows.
- Python 3.9+ on `PATH` as `python`. Standard library only — nothing to
  install.
- A secure credential store: macOS Keychain, Linux Secret Service
  (`secret-tool`), or PowerShell on Windows (SecretManagement if present,
  otherwise a user-scoped DPAPI blob). Without one, key storage refuses —
  there is no plaintext fallback.

## Install

Development / one session:

```bash
claude --plugin-dir /path/to/provider-switch
```

For every session, add this folder as a marketplace and install from it:

```bash
claude plugin marketplace add /path/to/provider-switch
claude plugin install provider-switch@provider-switch
```

Validate the plugin layout at any time:

```bash
claude plugin validate /path/to/provider-switch
```

## Quick start

```bash
python scripts/provider_switch.py add work --provider anthropic --model claude-sonnet-5-5
python scripts/provider_switch.py credential set work   # hidden prompt; type the key
python scripts/provider_switch.py switch work --yes
python scripts/provider_switch.py active
```

Inside Claude Code the same commands are available through the
`provider-switch` skill, e.g. `/provider-switch list`. The
`provider-credential` skill explains key entry and never asks you to paste
a key into the chat.

## Commands

| Command | Purpose |
|---|---|
| `list` | Profiles, with `*` on the active one |
| `active` | Active profile plus the effective settings values |
| `switch <name> [--yes] [--force-custom-host]` | Activate a profile |
| `add <name> --provider P --model M [--base-url U] [--credential REF] [--auth A] [--allow-http]` | Create a profile |
| `edit <name> [flags]` | Change fields on an existing profile |
| `remove <name> [--yes] [--delete-credential]` | Delete a profile (refuses if active) |
| `default [<name>]` | Show or set the fallback profile |
| `test <name> [--timeout S]` | `GET /v1/models` at the profile's endpoint |
| `validate` | Check profiles, credential store and settings file |
| `credential set <ref>` | Store a key at a hidden prompt (or one line on stdin) |
| `credential delete <ref>` | Delete a key from the credential store |
| `rollback` | Restore settings to their state before the last switch |
| `help` | Usage |

Global option: `--settings PATH` targets a settings file other than
`~/.claude/settings.json` (accepted before or after the subcommand).

## Providers

Bundled definitions live in `scripts/providers.json`:

| id | Auth | Notes |
|---|---|---|
| `anthropic` | `api_key` | `https://api.anthropic.com`; known host |
| `anthropic-compatible` | `bearer_token` | Requires `--base-url`; treated as a custom host |
| `bedrock` | `none` | Sets `CLAUDE_CODE_USE_BEDROCK=1`; uses your AWS credential chain |
| `vertex` | `none` | Sets `CLAUDE_CODE_USE_VERTEX=1`; uses your GCP credential chain |

To add another provider **without changing code**, create
`~/.claude/plugins/data/provider-switch/providers.json` with the same
shape:

```json
{
  "providers": [{
    "id": "acme",
    "label": "Acme Gateway",
    "auth": "bearer_token",
    "defaultBaseUrl": "https://gateway.acme.example",
    "requiresBaseUrl": false,
    "knownHosts": ["gateway.acme.example"],
    "models": [],
    "env": { "ACME_ROUTING": "team-a" },
    "capabilities": { "modelDiscovery": false, "testConnection": true }
  }]
}
```

Entries with an existing `id` replace the bundled one. `env` keys are
written into the managed `env` block and restored by `rollback`.

## Credential setup

Keys are entered through a hidden prompt (`getpass`), so they are not in
shell history, command arguments, terminal echo, or this tool's output.
Storing sends the key on stdin to the platform credential tool
(`security`, `secret-tool`, PowerShell). A profile stores only the
reference, e.g. `"credential": "work"`.

If no secure store is available the command fails with remediation steps.
It never writes the key to a configuration file.

Deletion removes the entry from the store. Backups, OS journaling, and
processes that already read the key may retain copies; secure erasure is
not guaranteed.

## When a switch takes effect

| What changed | When |
|---|---|
| `ANTHROPIC_BASE_URL`, other `env` values | New requests, including in a running session. Claude Code re-applies `env` when settings change. An in-flight request completes against the old endpoint. |
| `apiKeyHelper` / the stored key | Next helper refresh (5 minutes by default), or on the next 401/403. Restart Claude Code to pick it up immediately. |
| `ANTHROPIC_MODEL` | New requests — **unless** a top-level `model` setting or a `/model` choice is present, which takes precedence. `switch` warns when it sees one. |

`switch` also cancels `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` with
empty strings. This is deliberate: a set `ANTHROPIC_AUTH_TOKEN` makes
Claude Code ignore `apiKeyHelper`, and a stale `ANTHROPIC_API_KEY` would
outrank it. `rollback` restores both exactly.

## Configuration safety

Only these keys are ever written to the user settings file:
`apiKeyHelper`, and `env.ANTHROPIC_BASE_URL`, `env.ANTHROPIC_MODEL`,
`env.ANTHROPIC_API_KEY`, `env.ANTHROPIC_AUTH_TOKEN`, plus provider
`env` keys. Everything else is preserved byte-for-byte in meaning (the
file is re-serialized; comments were never valid JSON).

Each switch:

1. Reads and parses the settings file. Unparseable JSON is refused, never
   overwritten.
2. Copies the whole file to `~/.claude/plugins/data/provider-switch/backups/`.
3. Records the previous values of the managed keys in `last-transaction.json`.
4. Writes a temp file in the same directory, fsyncs it, then atomically
   replaces the settings file.

If a write is interrupted before the replace, the old file is intact. If
two processes write simultaneously, the atomic replace prevents
corruption; the last writer wins for the managed keys.

`rollback` restores exactly the managed keys captured in the transaction,
leaving unrelated edits made since the switch alone, and restores the
previous active profile.

**Precedence** (highest first): organization-managed settings, the
`--settings` CLI file, `.claude/settings.local.json`,
`.claude/settings.json`, `~/.claude/settings.json`. This tool writes only
the user file by default. Project and organization settings that set the
same `env` keys win over it — `switch` cannot and will not override them.
`--settings` exists for testing a different scope, not for bypassing
precedence.

## Testing a profile

`test <name>` makes the smallest useful request: `GET /v1/models` at the
profile's base URL. It prints the destination and a mask of the key
(`****abcd`) and distinguishes:

- HTTP 401/403 — invalid credential or insufficient permission
- HTTP 404 — endpoint does not expose `/v1/models`
- HTTP 429 — rate limited or out of quota
- other HTTP errors — with a redacted body excerpt
- connection errors — endpoint unreachable

It never falls back to another provider or credential.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `No secure credential store is available` | Linux: install `secret-tool` (libsecret) and unlock the keyring. macOS: ensure `security` is on `PATH`. Windows: ensure PowerShell is available. |
| `Refusing to edit ... not valid JSON` | Fix the settings file by hand; the tool will not overwrite it. |
| Switch succeeded but requests still fail with 401 | The helper cache may hold the old key for up to 5 minutes. Restart Claude Code, or wait for the next refresh. |
| Model did not change | A top-level `model` setting or `/model` choice outranks `ANTHROPIC_MODEL`; `active` shows it. |
| Provider still ignored | Managed or project settings set the same variables; check precedence above. |
| `Refusing to remove the active profile` | Switch to another profile or run `rollback` first. |
| `test` reports 404 | The gateway does not implement `/v1/models`; the credential may still be fine. Try switching and sending a real request. |

## Limitations

- Model switching uses `ANTHROPIC_MODEL`; the tool does not edit the
  top-level `model` setting because that would overwrite a user choice.
- macOS Keychain and Linux Secret Service backends are implemented but
  were **not executed** on the Windows machine this was built on (Windows
  DPAPI and the fail-closed path were exercised by tests).
- Credential deletion cannot guarantee secure erasure.
- `test` only exercises `GET /v1/models`; some gateways support chat but
  not model listing.
- No automatic fallback: if a provider fails, the tool reports it and
  leaves the configuration alone.
- Concurrent editors: writes are atomic, but a read-modify-write race can
  lose the other writer's change to the managed keys (last writer wins).

## Tests

```bash
python -m unittest discover -s tests -v
```

19 tests cover switching, config preservation, rollback, custom-host
confirmation, malformed settings, validation, redaction, credential-store
failure, stdin-only key passing, the helper's stdout contract, and the
provider extension path. Network and credential-store calls are mocked;
no test uses a real key or makes a real provider call.

## Layout

```
.claude-plugin/plugin.json      plugin manifest
.claude-plugin/marketplace.json local marketplace wrapper
skills/provider-switch/         command skill
skills/provider-credential/     secure key-entry skill
scripts/providers.json          provider registry (data)
scripts/config.py               profile store + validation
scripts/settings_editor.py      atomic, reversible settings edits
scripts/credential_store.py     OS credential store abstraction
scripts/api_key_helper.py       apiKeyHelper entry point
scripts/provider_switch.py      CLI
tests/                          unittest suite
docs/DESIGN.md                  architecture and platform findings
```
