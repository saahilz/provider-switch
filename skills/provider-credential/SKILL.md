---
name: provider-credential
description: Store or delete a provider API key securely for the active profile. Use when a profile has no credential, a key is invalid, or the user wants to rotate or remove a key. Never handles the key text itself.
allowed-tools: Bash
---

# Provider credentials

Keys go into the operating system's credential store (Keychain, Secret
Service, Windows Credential Manager, or a DPAPI-protected blob). They are
never written to settings files, profile files, logs, or shell arguments.

**You must never receive, echo, or echo back the key.** Do not ask the user
to paste it to you. Instead, tell them to run the entry command themselves
in their terminal, prefixed with `!` so it executes in their session:

```
! python "${CLAUDE_PLUGIN_ROOT}/scripts/provider_switch.py" credential set <ref>
```

The command prompts with hidden input (`getpass`), so the key does not
appear on screen, in shell history, or in command-line arguments. When the
process is not attached to a terminal, it reads a single line from stdin
instead - useful in a script, never in chat.

`<ref>` is the credential reference shown by `provider_switch.py list`
(by default it equals the profile name).

To remove a key:

```
! python "${CLAUDE_PLUGIN_ROOT}/scripts/provider_switch.py" credential delete <ref> --yes
```

Deletion removes the entry from the credential store. Explain the limit:
OS journaling, backups, and any process that already read the key may
still retain copies, so secure erasure cannot be guaranteed.

If the command reports that no secure store is available, do **not** offer
a config-file or plaintext alternative. Report the remediation text
(install `secret-tool` on Linux, ensure `security` on macOS, PowerShell on
Windows) and stop.
