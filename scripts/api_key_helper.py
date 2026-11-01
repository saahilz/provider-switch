#!/usr/bin/env python3
"""apiKeyHelper entry point: resolve the active profile's secret.

Claude Code runs this, sends its stdout as both X-Api-Key and
Authorization: Bearer, and caches it (5 min by default). Nothing here
writes to disk or logs; the only output on success is the secret itself
on stdout.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config  # noqa: E402
from credential_store import CredentialStore, CredentialStoreError  # noqa: E402


def main():
    try:
        data = config.load_profiles()
        name = data.get("active") or data.get("default")
        if not name:
            raise SystemExit("provider-switch: no active profile. Run: provider_switch.py switch <name>")
        profile = data["profiles"].get(name)
        if not profile:
            raise SystemExit("provider-switch: active profile %r no longer exists." % name)
        if profile.get("auth") == "none":
            raise SystemExit(
                "provider-switch: profile %r does not use an API key (auth: none)." % name
            )
        secret = CredentialStore().retrieve(profile["credential"])
        if not secret:
            raise SystemExit("provider-switch: stored credential for %r is empty." % name)
        sys.stdout.write(secret)
        sys.stdout.flush()
    except (config.ConfigError, CredentialStoreError) as exc:
        sys.stderr.write("provider-switch: %s\n" % exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
