"""Atomic, reversible edits to a Claude Code settings file.

Only the keys in MANAGED_KEYS are ever touched. Everything else in the
file is preserved. Every write is preceded by a full backup and a
transaction record, and is applied by atomic replace so a crash cannot
leave a half-written settings.json.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

DATA_DIR = Path.home() / ".claude" / "plugins" / "data" / "provider-switch"
BACKUP_DIR = DATA_DIR / "backups"
TXN_FILE = DATA_DIR / "last-transaction.json"

# Only these are ever written by the switcher.
MANAGED_ENV_KEYS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
                    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                    "CLAUDE_CODE_USE_FOUNDRY")
MANAGED_TOP_KEYS = ("apiKeyHelper",)


class SettingsError(RuntimeError):
    """Malformed or unwritable settings file."""


def default_user_settings():
    return Path.home() / ".claude" / "settings.json"


def read_settings(path):
    path = Path(path)
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SettingsError(
            "Refusing to edit %s: it is not valid JSON (%s).\n"
            "Fix it by hand first; the switcher will not overwrite a file it "
            "cannot parse." % (path, exc)
        )
    if not isinstance(data, dict):
        raise SettingsError("Refusing to edit %s: top level is not a JSON object." % path)
    return data


def write_settings(path, data):
    """Write via a temp file in the same directory, then atomic replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not os.access(path, os.W_OK):
        raise SettingsError("Refusing to edit %s: file is not writable." % path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".settings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def backup(path, stamp=None):
    """Copy the current settings file aside. Returns the backup path or None."""
    path = Path(path)
    if not path.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = stamp or time.strftime("%Y%m%dT%H%M%S")
    dest = BACKUP_DIR / ("%s.%s.json" % (path.name, stamp))
    shutil.copy2(path, dest)
    _restrict(dest)
    return dest


def record_transaction(previous_values, backup_path, settings_path, extra=None):
    """Store what we are about to change so rollback can undo exactly that."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    txn = {
        "settingsPath": str(settings_path),
        "previous": previous_values,
        "backup": str(backup_path) if backup_path else None,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if extra:
        txn.update(extra)
    tmp = TXN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(txn, indent=2), encoding="utf-8")
    os.replace(tmp, TXN_FILE)
    _restrict(TXN_FILE)
    return txn


def read_transaction():
    if not TXN_FILE.exists():
        return None
    try:
        return json.loads(TXN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def snapshot_managed(settings, extra_env_keys=()):
    """Capture current values of every managed key (including absence)."""
    snap = {"top": {}, "env": {}}
    for key in MANAGED_TOP_KEYS:
        snap["top"][key] = settings.get(key, None)
    env = settings.get("env") or {}
    for key in list(MANAGED_ENV_KEYS) + list(extra_env_keys):
        snap["env"][key] = env.get(key, None)
    return snap


def apply_managed(settings, top_values, env_values):
    """Return a new settings dict with only managed keys changed."""
    new = json.loads(json.dumps(settings))  # deep copy, preserves all other keys
    for key, value in top_values.items():
        if value is None:
            new.pop(key, None)
        else:
            new[key] = value
    env = new.get("env")
    if not isinstance(env, dict):
        env = {}
    for key, value in env_values.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    if env:
        new["env"] = env
    elif "env" in new:
        new.pop("env", None)
    return new


def restore_snapshot(settings, snapshot):
    """Undo apply_managed using a snapshot from snapshot_managed()."""
    return apply_managed(settings, snapshot.get("top", {}), snapshot.get("env", {}))


def _restrict(path):
    try:
        path.chmod(0o600)
    except Exception:
        pass
