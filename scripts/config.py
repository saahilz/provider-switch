"""Profile metadata store and validation.

Profiles hold a credential *reference*, never a secret. Provider
definitions are data (providers.json), so adding a provider never
requires touching this file.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

DATA_DIR = Path.home() / ".claude" / "plugins" / "data" / "provider-switch"
PROFILES_FILE = DATA_DIR / "profiles.json"
PROVIDERS_FILE = Path(__file__).parent / "providers.json"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


class ConfigError(RuntimeError):
    """Invalid profile or provider data."""


def load_providers(path=None):
    """Bundled providers, optionally extended/overridden by a user data file.

    The extension path is data, not code: drop a providers.json with the
    same shape at DATA_DIR/providers.json. Entries with an existing id
    replace the bundled definition.
    """
    path = Path(path or PROVIDERS_FILE)
    data = json.loads(path.read_text(encoding="utf-8"))
    providers = {p["id"]: p for p in data["providers"]}
    if path == PROVIDERS_FILE:
        extra = DATA_DIR / "providers.json"
        if extra.exists():
            try:
                for p in json.loads(extra.read_text(encoding="utf-8")).get("providers", []):
                    providers[p["id"]] = p
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise ConfigError("%s is malformed: %s" % (extra, exc))
    return providers


def load_profiles(path=None):
    path = Path(path or PROFILES_FILE)
    if not path.exists():
        return {"version": 1, "active": None, "default": None, "profiles": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError("profiles.json is not valid JSON: %s" % exc)
    data.setdefault("version", 1)
    data.setdefault("profiles", {})
    data.setdefault("active", None)
    data.setdefault("default", None)
    return data


def save_profiles(data, path=None):
    path = Path(path or PROFILES_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except Exception:
        pass
    tmp.replace(path)
    return path


def validate_name(name):
    if not name or not NAME_RE.match(name):
        raise ConfigError(
            "Invalid profile name %r. Use lowercase letters, digits, dot, dash or "
            "underscore, starting with a letter or digit (max 63 chars)." % name
        )
    return name


def validate_url(url, allow_http=False):
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise ConfigError("Base URL must start with https:// (or http:// for localhost).")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        if not allow_http:
            raise ConfigError(
                "Refusing an http:// base URL for a remote host - credentials would travel "
                "in clear text. Use https://, or pass --allow-http if you control the network."
            )
    if not parsed.hostname:
        raise ConfigError("Base URL has no host: %r" % url)
    return url.rstrip("/")


def build_profile(name, provider_id, providers, credential_ref=None,
                  model=None, base_url=None, auth=None, allow_http=False,
                  extra_env=None):
    validate_name(name)
    if provider_id not in providers:
        raise ConfigError(
            "Unknown provider %r. Known providers: %s"
            % (provider_id, ", ".join(sorted(providers)))
        )
    provider = providers[provider_id]

    if provider.get("requiresBaseUrl") and not base_url:
        raise ConfigError(
            "Provider %r requires --base-url (it has no default endpoint)." % provider_id
        )
    if base_url:
        base_url = validate_url(base_url, allow_http=allow_http)
    else:
        base_url = provider.get("defaultBaseUrl")

    if not model:
        raise ConfigError("A profile needs a model (--model).")

    profile_env = dict(provider.get("env") or {})
    profile_env.update(extra_env or {})
    return {
        "id": name,
        "provider": provider_id,
        "label": provider.get("label", provider_id),
        "credential": credential_ref or name,
        "auth": auth or provider.get("auth", "api_key"),
        "model": model,
        "baseUrl": base_url,
        "env": profile_env,
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def is_custom_host(profile, providers):
    """True when the base URL host is not the provider's known host."""
    base = profile.get("baseUrl")
    if not base:
        return False
    host = urlparse(base).hostname or ""
    known = providers.get(profile.get("provider"), {}).get("knownHosts") or []
    return host not in known


def managed_env_for(profile, helper_command):
    """Translate a profile into the exact settings values to write.

    ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN are explicitly cancelled:
    the docs state apiKeyHelper is ignored while ANTHROPIC_AUTH_TOKEN is
    set, and a stale ANTHROPIC_API_KEY would otherwise outrank the helper.
    Profiles with auth "none" (Bedrock/Vertex) get no helper at all -
    they authenticate through the cloud provider's own credential chain.
    """
    env = {
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_AUTH_TOKEN": "",
        "ANTHROPIC_MODEL": profile["model"],
        "ANTHROPIC_BASE_URL": profile.get("baseUrl") or None,
    }
    for key, value in (profile.get("env") or {}).items():
        env[key] = value
    helper = None if profile.get("auth") == "none" else helper_command
    return {"apiKeyHelper": helper}, env
