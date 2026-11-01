#!/usr/bin/env python3
"""provider-switch: manage provider profiles and switch Claude Code between them.

Secrets live in the OS credential store. This command only ever writes
`apiKeyHelper` and a small allow-listed `env` subset into the settings
file, after taking a backup and recording a transaction.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config  # noqa: E402
import settings_editor  # noqa: E402
from credential_store import (  # noqa: E402
    CredentialStore, CredentialStoreError, UNAVAILABLE_MESSAGE,
)

VERSION = "0.1.0"

SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(x-api-key:\s*)\S+"),
)


def redact(text, secret=None):
    text = str(text)
    if secret:
        text = text.replace(secret, "[redacted]")
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "[redacted]", text)
    return text


def mask(secret):
    return "****" + secret[-4:] if secret and len(secret) > 4 else "****"


def confirm(prompt, assume_yes):
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("Refusing without confirmation in a non-interactive shell. Re-run with --yes.",
              file=sys.stderr)
        return False
    try:
        answer = input(prompt + " [y/N] ").strip().lower()
    except EOFError:
        print("Refusing without confirmation. Re-run with --yes.", file=sys.stderr)
        return False
    return answer in ("y", "yes")


def helper_command():
    """Absolute paths, because ${CLAUDE_PLUGIN_ROOT} is not resolved here."""
    py = str(Path(sys.executable).resolve())
    helper = str(Path(__file__).resolve().parent / "api_key_helper.py")
    if os.name == "nt":
        return '"%s" "%s"' % (py, helper)
    return "%s %s" % (shlex.quote(py), shlex.quote(helper))


def store():
    return CredentialStore()


def load_all():
    return config.load_profiles(), config.load_providers()


def require_profile(data, name):
    profile = data["profiles"].get(name)
    if not profile:
        raise config.ConfigError(
            "No profile %r. Known profiles: %s"
            % (name, ", ".join(sorted(data["profiles"])) or "(none)")
        )
    return profile


def settings_path(args):
    return Path(args.settings) if args.settings else settings_editor.default_user_settings()


def print_effect_timing():
    print("Takes effect: API key/base URL/env changes apply to new requests, including in")
    print("running sessions (Claude Code re-applies `env` when settings change). A stored key")
    print("is re-read on the helper's next refresh (5 min default) or on the next 401/403.")
    print("A top-level `model` setting or a /model choice outranks ANTHROPIC_MODEL.")


# ---------------------------------------------------------------- commands

def cmd_list(args):
    data, providers = load_all()
    if not data["profiles"]:
        print("No profiles yet. Add one: provider_switch.py add work --provider anthropic --model <model>")
        return 0
    current = data.get("active") or data.get("default")
    rows = [("", "NAME", "PROVIDER", "MODEL", "HOST", "CREDENTIAL")]
    for name in sorted(data["profiles"]):
        p = data["profiles"][name]
        rows.append(("*" if name == current else "", name, p["provider"], p["model"],
                     p.get("baseUrl") or "(provider default)", p["credential"]))
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    if current:
        print("\n* = active profile: %s" % current)
    return 0


def cmd_active(args):
    data, providers = load_all()
    name = data.get("active") or data.get("default")
    if not name:
        print("No active profile.")
        return 0
    p = require_profile(data, name)
    print("Active profile: %s" % name)
    print("  provider:   %s (%s)" % (p["provider"], p.get("label", p["provider"])))
    print("  model:      %s" % p["model"])
    print("  base URL:   %s" % (p.get("baseUrl") or "(provider default)"))
    print("  credential: %s" % p["credential"])
    print("  source:     %s" % ("switch" if data.get("active") else "default profile"))
    if config.is_custom_host(p, providers):
        print("  WARNING:    custom host - credentials are sent to this host.")
    try:
        s = settings_editor.read_settings(settings_path(args))
    except settings_editor.SettingsError as exc:
        print("  settings:   unreadable (%s)" % exc)
        return 0
    env = s.get("env") or {}
    print("  settings:   apiKeyHelper=%s, ANTHROPIC_BASE_URL=%s, ANTHROPIC_MODEL=%s"
          % ("set" if s.get("apiKeyHelper") else "unset",
             env.get("ANTHROPIC_BASE_URL") or "(unset)", env.get("ANTHROPIC_MODEL") or "(unset)"))
    if s.get("model"):
        print("  note:       top-level `model` = %r overrides ANTHROPIC_MODEL." % s["model"])
    return 0


def cmd_switch(args):
    data, providers = load_all()
    p = require_profile(data, args.name)
    if p.get("auth") != "none":
        try:
            secret = store().retrieve(p["credential"])
        except CredentialStoreError as exc:
            raise config.ConfigError(
                "No usable credential for profile %r: %s\n"
                "Store one first: provider_switch.py credential set %s" % (args.name, exc, p["credential"])
            )
        if not secret:
            raise config.ConfigError("Stored credential for %r is empty." % args.name)
    if config.is_custom_host(p, providers) and not args.force_custom:
        print("WARNING: profile %r sends your credential to %s, which is not a known host"
              % (args.name, p.get("baseUrl")))
        print("for provider %r. Only continue if you trust that endpoint." % p["provider"])
        if not confirm("Switch anyway?", args.yes):
            return 1
    path = settings_path(args)
    settings = settings_editor.read_settings(path)
    top_values, env_values = config.managed_env_for(p, helper_command())
    snapshot = settings_editor.snapshot_managed(settings, env_values.keys())
    backup_path = settings_editor.backup(path)
    settings_editor.record_transaction(snapshot, backup_path, path,
                                       {"previousActive": data.get("active")})
    settings_editor.write_settings(path, settings_editor.apply_managed(settings, top_values, env_values))
    data["active"] = args.name
    data["profiles"][args.name] = p
    config.save_profiles(data)
    print("Switched to %s (%s, model %s)." % (args.name, p["provider"], p["model"]))
    print("Settings: %s  (backup: %s)" % (path, backup_path or "none - file did not exist"))
    if settings.get("model") or settings.get("modelSettings"):
        print("Note: a top-level `model`/`modelSettings` is present and keeps precedence over")
        print("ANTHROPIC_MODEL. Use /model or edit that setting to change the model.")
    print_effect_timing()
    return 0


def _profile_from_args(args, providers, data, existing=None):
    provider_id = args.provider or (existing or {}).get("provider")
    if not provider_id:
        raise config.ConfigError("--provider is required.")
    provider = providers.get(provider_id)
    if not provider:
        raise config.ConfigError("Unknown provider %r. Known: %s"
                                 % (provider_id, ", ".join(sorted(providers))))
    model = args.model or (existing or {}).get("model")
    if not model:
        raise config.ConfigError("--model is required.")
    base_url = args.base_url
    if base_url is None:
        if existing:
            base_url = existing.get("baseUrl")
            if provider_id != existing.get("provider"):
                base_url = provider.get("defaultBaseUrl")
        else:
            base_url = provider.get("defaultBaseUrl")
    if provider.get("requiresBaseUrl") and not base_url:
        raise config.ConfigError("Provider %r requires --base-url." % provider_id)
    if base_url:
        base_url = config.validate_url(base_url, allow_http=args.allow_http)
    credential = (args.credential or (existing or {}).get("credential") or args.name)
    profile = config.build_profile(
        args.name, provider_id, providers, credential_ref=credential,
        model=model, base_url=base_url, auth=args.auth or (existing or {}).get("auth"),
        allow_http=args.allow_http,
    )
    if existing:
        profile["createdAt"] = existing.get("createdAt", profile["createdAt"])
    return profile


def cmd_add(args):
    data, providers = load_all()
    if args.name in data["profiles"]:
        raise config.ConfigError("Profile %r already exists. Use `edit`." % args.name)
    profile = _profile_from_args(args, providers, data)
    data["profiles"][args.name] = profile
    if not data.get("default"):
        data["default"] = args.name
    config.save_profiles(data)
    print("Added profile %r (provider %s, model %s)." % (args.name, profile["provider"], profile["model"]))
    if profile.get("auth") != "none":
        print("Next: store its key securely:  provider_switch.py credential set %s" % profile["credential"])
    print("Then:  provider_switch.py switch %s" % args.name)
    return 0


def cmd_edit(args):
    data, providers = load_all()
    existing = require_profile(data, args.name)
    profile = _profile_from_args(args, providers, data, existing)
    data["profiles"][args.name] = profile
    config.save_profiles(data)
    print("Updated profile %r." % args.name)
    if data.get("active") == args.name:
        print("This profile is active - run `switch %s` again to apply the change." % args.name)
    return 0


def cmd_remove(args):
    data, providers = load_all()
    p = require_profile(data, args.name)
    if data.get("active") == args.name:
        print("Refusing to remove the active profile. Switch to another profile first,",
              file=sys.stderr)
        print("or run `rollback` to restore the previous configuration.", file=sys.stderr)
        return 1
    if not confirm("Remove profile %r?" % args.name, args.yes):
        return 1
    del data["profiles"][args.name]
    if data.get("default") == args.name:
        data["default"] = None
    config.save_profiles(data)
    print("Removed profile %r. Its settings are untouched." % args.name)
    if args.delete_credential:
        removed = store().delete(p["credential"])
        print("Credential %r %s." % (p["credential"],
                                     "deleted from the credential store" if removed
                                     else "was not present in the credential store"))
        print("Note: backups and OS journaling may retain old copies; secure erasure is not guaranteed.")
    return 0


def cmd_default(args):
    data, _ = load_all()
    if not args.name:
        print("Default profile: %s" % (data.get("default") or "(none)"))
        print("Active profile:  %s" % (data.get("active") or "(falls back to default)"))
        return 0
    require_profile(data, args.name)
    data["default"] = args.name
    config.save_profiles(data)
    print("Default profile is now %r." % args.name)
    if data.get("active"):
        print("Active profile is still %r; `rollback` or `switch %s` to change it."
              % (data["active"], args.name))
    return 0


def cmd_rollback(args):
    txn = settings_editor.read_transaction()
    if not txn:
        print("Nothing to roll back: no transaction recorded.", file=sys.stderr)
        return 1
    path = Path(txn["settingsPath"])
    settings = settings_editor.read_settings(path)
    settings_editor.write_settings(
        path, settings_editor.restore_snapshot(settings, txn["previous"]))
    data = config.load_profiles()
    data["active"] = txn.get("previousActive")
    config.save_profiles(data)
    settings_editor.TXN_FILE.unlink(missing_ok=True)
    print("Rolled back %s to its state before the last switch (%s)." % (path, txn["at"]))
    print("Full backup kept at: %s" % (txn.get("backup") or "(none)"))
    print("Active profile is now: %s" % (data.get("active") or "(falls back to default)"))
    return 0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the credential to wherever it points."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, "redirect refused (it would resend the credential)",
            headers, fp)


TEST_OPENER = urllib.request.build_opener(_NoRedirect)


def cmd_test(args):
    data, providers = load_all()
    p = require_profile(data, args.name)
    base = p.get("baseUrl") or providers.get(p["provider"], {}).get("defaultBaseUrl")
    if not base:
        raise config.ConfigError("Profile %r has no base URL to test." % args.name)
    url = base.rstrip("/") + "/v1/models"
    headers = {"anthropic-version": "2023-06-01", "accept": "application/json"}
    secret = None
    if p.get("auth") != "none":
        try:
            secret = store().retrieve(p["credential"])
        except CredentialStoreError as exc:
            raise config.ConfigError("No usable credential for %r: %s" % (args.name, exc))
        if p.get("auth") == "bearer_token":
            headers["authorization"] = "Bearer " + secret
        else:
            headers["x-api-key"] = secret
    print("Testing %s -> %s" % (args.name, url))
    if secret:
        print("Authenticating with credential %r (%s)" % (p["credential"], mask(secret)))
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with TEST_OPENER.open(req, timeout=args.timeout) as resp:
            body = resp.read(2000).decode("utf-8", "replace")
            print("OK: HTTP %d from %s" % (resp.status, resp.geturl()))
            try:
                models = [m.get("id") for m in json.loads(body).get("data", [])]
                if models:
                    print("Models visible: %s" % ", ".join(str(m) for m in models[:10]))
                    if p["model"] not in models:
                        print("Note: configured model %r is not in that list - the endpoint may")
                        print("use different model ids, or may not list models at all.")
            except Exception:
                pass
        return 0
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(400).decode("utf-8", "replace")
        except Exception:
            pass
        if exc.code in (401, 403):
            print("FAIL: authentication/permission rejected (HTTP %d). The credential may be" % exc.code)
            print("invalid, expired, or not allowed to use this model. %s" % redact(detail, secret))
        elif exc.code == 404:
            print("FAIL: endpoint not found (HTTP 404). This host may not support /v1/models.")
        elif exc.code == 429:
            print("FAIL: rate limited or out of quota (HTTP 429).")
        else:
            print("FAIL: HTTP %d from %s. %s" % (exc.code, url, redact(detail, secret)))
        return 1
    except urllib.error.URLError as exc:
        print("FAIL: endpoint unreachable (%s). %s" % (url, redact(exc.reason, secret)))
        return 1
    except Exception as exc:
        print("FAIL: %s" % redact(exc, secret))
        return 1


def cmd_validate(args):
    data, providers = load_all()
    problems = 0
    try:
        settings_editor.read_settings(settings_path(args))
        print("settings: parseable")
    except settings_editor.SettingsError as exc:
        print("settings: ERROR %s" % exc)
        problems += 1
    s = store()
    print("credential store: %s (%s)" % (s.describe(), "available" if s.available() else "UNAVAILABLE"))
    if not data["profiles"]:
        print("profiles: none")
    for name, p in sorted(data["profiles"].items()):
        issues = []
        provider = providers.get(p.get("provider"))
        if not provider:
            issues.append("unknown provider %r" % p.get("provider"))
        else:
            if provider.get("requiresBaseUrl") and not p.get("baseUrl"):
                issues.append("missing base URL")
            if p.get("baseUrl") and not provider.get("knownHosts"):
                pass
        if not p.get("model"):
            issues.append("missing model")
        if p.get("auth") != "none" and s.available():
            try:
                if not s.retrieve(p["credential"]):
                    issues.append("empty credential")
            except CredentialStoreError:
                issues.append("no stored credential for %r" % p["credential"])
        print("profile %s: %s" % (name, "OK" if not issues else "; ".join(issues)))
        problems += len(issues)
    print("Result: %s" % ("OK" if not problems else "%d problem(s)" % problems))
    return 0 if not problems else 1


def cmd_credential(args):
    s = store()
    if args.action == "set":
        if not s.available():
            raise CredentialStoreError(UNAVAILABLE_MESSAGE)
        if sys.stdin.isatty() and sys.stderr.isatty():
            secret = getpass.getpass("Paste the API key for %r (input hidden): " % args.ref)
        else:
            secret = sys.stdin.readline()
        secret = (secret or "").strip()
        if not secret:
            raise CredentialStoreError("No key entered; nothing stored.")
        s.store(args.ref, secret)
        print("Stored credential %r in %s as %s" % (args.ref, s.describe(), mask(secret)))
        print("The key was not written to the settings file or to this command's arguments.")
        return 0
    if args.action == "delete":
        if not confirm("Delete stored credential %r?" % args.ref, args.yes):
            return 1
        removed = s.delete(args.ref)
        print("Credential %r %s." % (args.ref, "deleted" if removed else "was not present"))
        print("Limits: the OS store may keep journaled copies; secure erasure is not guaranteed.")
        return 0
    raise config.ConfigError("Unknown credential action %r." % args.action)


def cmd_help(args):
    print(HELP)
    return 0


HELP = """provider-switch %s - switch Claude Code between provider profiles

  list                       list profiles, marking the active one
  active                     show the active profile and effective settings
  switch <name> [--yes]      activate a profile (--force-custom-host to skip the warning)
  add <name> --provider P --model M [--base-url U] [--credential REF] [--auth A]
  edit <name> [same flags]   change profile fields
  remove <name> [--yes] [--delete-credential]
  default [<name>]           show or set the default profile
  test <name>                GET /v1/models against the profile's endpoint
  validate                   check profiles, credential store and settings file
  credential set <ref>       enter a key at a hidden prompt (or pipe one line on stdin)
  credential delete <ref>    remove a stored key
  rollback                   restore the settings as they were before the last switch

Global: --settings PATH   edit a settings file other than ~/.claude/settings.json

Secrets are stored only in the OS credential store; profiles keep a reference.
Nothing is ever printed in full. No automatic fallback between providers.
""" % VERSION


def build_parser():
    # --settings is accepted both before and after the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--settings", help="settings file to edit (default: ~/.claude/settings.json)")
    p = argparse.ArgumentParser(prog="provider_switch.py", add_help=True,
                                parents=[common],
                                description="Switch Claude Code between provider profiles.")
    sub = p.add_subparsers(dest="command")

    def add(name, **kw):
        sp = sub.add_parser(name, parents=[common], **kw)
        return sp

    add("list").set_defaults(func=cmd_list)
    add("active").set_defaults(func=cmd_active)

    sp = add("switch")
    sp.add_argument("name")
    sp.add_argument("--yes", action="store_true")
    sp.add_argument("--force-custom-host", action="store_true", dest="force_custom")
    sp.set_defaults(func=cmd_switch)

    def add_profile_flags(sp):
        sp.add_argument("name")
        sp.add_argument("--provider")
        sp.add_argument("--model")
        sp.add_argument("--base-url", dest="base_url")
        sp.add_argument("--credential")
        sp.add_argument("--auth", choices=("api_key", "bearer_token", "none"))
        sp.add_argument("--allow-http", action="store_true", dest="allow_http")

    sp = add("add")
    add_profile_flags(sp)
    sp.set_defaults(func=cmd_add)
    sp = add("edit")
    add_profile_flags(sp)
    sp.set_defaults(func=cmd_edit)

    sp = add("remove")
    sp.add_argument("name")
    sp.add_argument("--yes", action="store_true")
    sp.add_argument("--delete-credential", action="store_true", dest="delete_credential")
    sp.set_defaults(func=cmd_remove)

    sp = add("default")
    sp.add_argument("name", nargs="?")
    sp.set_defaults(func=cmd_default)

    sp = add("test")
    sp.add_argument("name")
    sp.add_argument("--timeout", type=float, default=15.0)
    sp.set_defaults(func=cmd_test)

    add("validate").set_defaults(func=cmd_validate)
    add("rollback").set_defaults(func=cmd_rollback)
    add("help").set_defaults(func=cmd_help)

    sp = add("credential")
    sp.add_argument("action", choices=("set", "delete"))
    sp.add_argument("ref")
    sp.add_argument("--yes", action="store_true")
    sp.set_defaults(func=cmd_credential)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except (config.ConfigError, settings_editor.SettingsError, CredentialStoreError) as exc:
        print("error: %s" % redact(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
