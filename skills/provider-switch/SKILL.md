---
name: provider-switch
description: List, add, switch, test and roll back AI provider profiles for Claude Code. Use when the user asks to change provider, endpoint, model, or API key; to see which profile is active; or to undo a provider switch.
argument-hint: "[list|active|switch <name>|add <name> --provider P --model M|edit|remove|test|default|validate|rollback|help]"
allowed-tools: Bash
---

# Provider Switch

Run the CLI with the user's arguments:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/provider_switch.py" $ARGUMENTS
```

If no arguments were given, run `list` and then `active` so the user sees
both the profiles and the effective configuration.

Rules for this skill:

- **Never ask the user to paste an API key into the chat.** Key entry is a
  separate, hidden prompt they run themselves (see the `provider-credential`
  skill). If a key is missing, tell them the exact command to run with a
  leading `!` so it executes in their terminal.
- Show command output as-is. It is already redacted and never contains a
  full secret.
- Do not add `--yes` to `remove` or to a custom-host `switch` on your own
  initiative; ask the user first. `--yes` is fine for `switch` to a known
  host.
- Do not run `rollback` unless the user asks to undo the last switch.
- Do not edit `~/.claude/settings.json` directly for provider changes; use
  the CLI so the backup and transaction record exist.
- If a command fails, report the error and the suggested next command. Do
  not fall back to another provider or credential.

Common commands (all run through the CLI path above):

| Task | Arguments |
|---|---|
| See profiles | `list` |
| See active profile | `active` |
| Switch | `switch work --yes` |
| Add | `add work --provider anthropic --model claude-sonnet-5-5` |
| Add a gateway | `add gw --provider anthropic-compatible --base-url https://host --model <id>` |
| Change a field | `edit work --model claude-opus-5-5` |
| Test the endpoint | `test work` |
| Set the default | `default work` |
| Check everything | `validate` |
| Undo the last switch | `rollback` |

When a switch takes effect: `apiKeyHelper` and `env` changes apply to new
requests, including in a running session. A stored key is re-read on the
helper's next refresh (5 minutes by default) or on the next 401/403. A
top-level `model` setting or a `/model` choice outranks `ANTHROPIC_MODEL`.
