"""Tests for provider switching. No real keys, no network, no real OS store."""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import api_key_helper  # noqa: E402
import config  # noqa: E402
import credential_store  # noqa: E402
import provider_switch  # noqa: E402
import settings_editor  # noqa: E402

SECRET = "sk-test-SECRET-abcdef123456"
MODEL = "claude-sonnet-5-5"


class FakeStore:
    def __init__(self):
        self.secrets = {"work": SECRET, "gw": SECRET}

    def available(self):
        return True

    def describe(self):
        return "fake store"

    def retrieve(self, ref):
        if ref not in self.secrets:
            raise credential_store.CredentialStoreError("missing %r" % ref)
        return self.secrets[ref]

    def store(self, ref, secret):
        self.secrets[ref] = secret

    def delete(self, ref):
        return self.secrets.pop(ref, None) is not None


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings_path = root / "settings.json"
        self.profiles_path = root / "profiles.json"
        self.providers_path = root / "providers.json"
        shutil.copy(config.PROVIDERS_FILE, self.providers_path)
        self.fake = FakeStore()
        self.patches = [
            patch.object(config, "PROFILES_FILE", self.profiles_path),
            patch.object(config, "PROVIDERS_FILE", self.providers_path),
            patch.object(config, "DATA_DIR", root / "data"),
            patch.object(settings_editor, "DATA_DIR", root / "data"),
            patch.object(settings_editor, "BACKUP_DIR", root / "data" / "backups"),
            patch.object(settings_editor, "TXN_FILE", root / "data" / "txn.json"),
            patch.object(credential_store, "CRED_DIR", root / "creds"),
            patch.object(provider_switch, "store", lambda: self.fake),
            patch.object(api_key_helper, "CredentialStore", lambda: self.fake),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    # -- fixtures -----------------------------------------------------------

    def write_settings(self, data):
        self.settings_path.write_text(json.dumps(data), encoding="utf-8")
        return data

    def seed(self, name="work", **overrides):
        profile = {
            "id": name,
            "provider": "anthropic",
            "label": "Anthropic",
            "credential": name,
            "auth": "api_key",
            "model": MODEL,
            "baseUrl": "https://api.anthropic.com",
            "env": {},
        }
        profile.update(overrides)
        data = {"version": 1, "active": None, "default": name,
                "profiles": {name: profile}}
        self.profiles_path.write_text(json.dumps(data), encoding="utf-8")
        return profile

    def add_profile(self, name, **fields):
        data = json.loads(self.profiles_path.read_text(encoding="utf-8"))
        data["profiles"][name] = {
            "id": name, "provider": "anthropic", "label": "Anthropic",
            "credential": name, "auth": "api_key", "model": MODEL,
            "baseUrl": "https://api.anthropic.com", "env": {},
        }
        data["profiles"][name].update(fields)
        self.profiles_path.write_text(json.dumps(data), encoding="utf-8")

    def run_cli(self, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        argv = list(argv) + ["--settings", str(self.settings_path)]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if stdin is None:
                code = provider_switch.main(argv)
            else:
                with patch.object(sys, "stdin", stdin):
                    code = provider_switch.main(argv)
        return code, out.getvalue(), err.getvalue()

    def settings(self):
        return json.loads(self.settings_path.read_text(encoding="utf-8"))

    def profiles(self):
        return json.loads(self.profiles_path.read_text(encoding="utf-8"))

    # -- tests --------------------------------------------------------------

    def test_switch_writes_managed_keys_and_preserves_everything_else(self):
        self.write_settings({
            "theme": "dark",
            "permissions": {"allow": ["Bash(ls)"]},
            "env": {"FOO": "bar", "ANTHROPIC_AUTH_TOKEN": "old-token"},
        })
        self.seed()

        code, out, err = self.run_cli("switch", "work")

        self.assertEqual(code, 0, err)
        s = self.settings()
        self.assertEqual(s["theme"], "dark")
        self.assertEqual(s["permissions"], {"allow": ["Bash(ls)"]})
        self.assertEqual(s["env"]["FOO"], "bar")
        self.assertEqual(s["env"]["ANTHROPIC_AUTH_TOKEN"], "")
        self.assertEqual(s["env"]["ANTHROPIC_API_KEY"], "")
        self.assertEqual(s["env"]["ANTHROPIC_MODEL"], MODEL)
        self.assertEqual(s["env"]["ANTHROPIC_BASE_URL"], "https://api.anthropic.com")
        self.assertIn("api_key_helper.py", s["apiKeyHelper"])
        self.assertEqual(self.profiles()["active"], "work")
        self.assertIn("Switched to work", out)
        self.assertIn("Takes effect", out)
        self.assertNotIn(SECRET, out + err)

    def test_rollback_restores_exact_previous_state(self):
        original = self.write_settings({
            "theme": "dark",
            "env": {"FOO": "bar", "ANTHROPIC_AUTH_TOKEN": "old-token"},
        })
        self.seed()
        self.run_cli("switch", "work")

        code, out, err = self.run_cli("rollback")

        self.assertEqual(code, 0, err)
        self.assertEqual(self.settings(), original)
        self.assertIsNone(self.profiles()["active"])
        self.assertIn("Rolled back", out)

    def test_switch_without_credential_does_not_touch_settings(self):
        original = self.write_settings({"theme": "light"})
        self.seed(credential="missing")

        code, _, err = self.run_cli("switch", "work")

        self.assertEqual(code, 1)
        self.assertIn("credential set missing", err)
        self.assertEqual(self.settings(), original)

    def test_custom_host_requires_confirmation(self):
        self.write_settings({"theme": "light"})
        self.seed("gw", provider="anthropic-compatible", auth="bearer_token",
                  baseUrl="https://gw.example.com", label="Gateway")

        code, out, err = self.run_cli("switch", "gw")
        self.assertEqual(code, 1)
        self.assertIn("not a known host", out + err)
        self.assertNotIn("apiKeyHelper", self.settings())

        code, _, err = self.run_cli("switch", "gw", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.settings()["env"]["ANTHROPIC_BASE_URL"], "https://gw.example.com")

    def test_malformed_settings_are_never_overwritten(self):
        self.settings_path.write_text("{ this is not json", encoding="utf-8")
        self.seed()

        code, _, err = self.run_cli("switch", "work")

        self.assertEqual(code, 1)
        self.assertIn("not valid JSON", err)
        self.assertEqual(self.settings_path.read_text(encoding="utf-8"), "{ this is not json")

    def test_rollback_without_transaction_is_an_error(self):
        code, _, err = self.run_cli("rollback")
        self.assertEqual(code, 1)
        self.assertIn("Nothing to roll back", err)

    def test_remove_active_profile_is_refused(self):
        self.write_settings({})
        self.seed()
        self.run_cli("switch", "work")
        code, _, err = self.run_cli("remove", "work", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("active profile", err)
        self.assertIn("work", self.profiles()["profiles"])

    def test_validate_flags_missing_credential(self):
        self.write_settings({})
        self.seed()
        self.fake.secrets.clear()
        code, out, _ = self.run_cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("no stored credential", out)
        self.assertNotIn(SECRET, out)

    def test_active_report_shows_profile(self):
        self.write_settings({})
        self.seed()
        self.run_cli("switch", "work")
        code, out, _ = self.run_cli("active")
        self.assertEqual(code, 0)
        self.assertIn("Active profile: work", out)
        self.assertIn("ANTHROPIC_MODEL=claude-sonnet-5-5", out)

    def test_provider_extension_is_data_only(self):
        providers = json.loads(self.providers_path.read_text(encoding="utf-8"))
        providers["providers"].append({
            "id": "acme", "label": "Acme Gateway", "auth": "bearer_token",
            "defaultBaseUrl": "https://acme.example/api", "requiresBaseUrl": False,
            "knownHosts": ["acme.example"], "models": [],
            "env": {"SOME_PROVIDER_FLAG": "1"},
            "capabilities": {"modelDiscovery": False, "testConnection": True},
        })
        self.providers_path.write_text(json.dumps(providers), encoding="utf-8")
        loaded = config.load_providers(self.providers_path)
        profile = config.build_profile("acme1", "acme", loaded,
                                       model="some-model", base_url=None)
        self.assertEqual(profile["baseUrl"], "https://acme.example/api")
        self.assertFalse(config.is_custom_host(profile, loaded))
        top, env = config.managed_env_for(profile, "helper")
        self.assertEqual(env["SOME_PROVIDER_FLAG"], "1")
        self.assertEqual(top["apiKeyHelper"], "helper")

    def test_no_helper_for_cloud_providers(self):
        loaded = config.load_providers(self.providers_path)
        profile = config.build_profile("b1", "bedrock", loaded, model="m", auth="none")
        top, env = config.managed_env_for(profile, "helper")
        self.assertIsNone(top["apiKeyHelper"])
        self.assertEqual(env["CLAUDE_CODE_USE_BEDROCK"], "1")

    def test_user_provider_file_extends_bundled_providers(self):
        data_dir = Path(config.DATA_DIR)
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "providers.json").write_text(json.dumps({"providers": [{
            "id": "acme", "label": "Acme", "auth": "bearer_token",
            "defaultBaseUrl": "https://acme.example", "requiresBaseUrl": False,
            "knownHosts": ["acme.example"], "models": [], "capabilities": {},
        }]}), encoding="utf-8")
        loaded = config.load_providers()
        self.assertIn("acme", loaded)
        self.assertIn("anthropic", loaded)


class ValidationTests(unittest.TestCase):
    def test_rejects_plain_http_to_remote_host(self):
        with self.assertRaises(config.ConfigError):
            config.validate_url("http://example.com")
        self.assertEqual(config.validate_url("http://127.0.0.1:8080"),
                         "http://127.0.0.1:8080")

    def test_rejects_bad_names_and_unknown_providers(self):
        providers = config.load_providers()
        with self.assertRaises(config.ConfigError):
            config.validate_name("Bad Name")
        with self.assertRaises(config.ConfigError):
            config.build_profile("x", "nope", providers, model="m")
        with self.assertRaises(config.ConfigError):
            config.build_profile("gw", "anthropic-compatible", providers, model="m")

    def test_redaction_removes_secret_and_key_patterns(self):
        text = provider_switch.redact(
            "key=%s header=Bearer abcdefghijklmnop sk-ant-1234567890" % SECRET, SECRET)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("sk-ant-1234567890", text)
        self.assertNotIn("abcdefghijklmnop", text)

    def test_mask_never_reveals_more_than_four_characters(self):
        masked = provider_switch.mask(SECRET)
        self.assertEqual(masked, "****" + SECRET[-4:])
        self.assertLessEqual(len(masked), 8)

    def test_test_connection_refuses_redirects(self):
        req = provider_switch.urllib.request.Request(
            "https://gw.example.com/v1/models",
            headers={"x-api-key": SECRET})
        handler = provider_switch._NoRedirect()
        with self.assertRaises(provider_switch.urllib.error.HTTPError):
            handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example/")


class CredentialStoreTests(unittest.TestCase):
    def test_unavailable_store_refuses_instead_of_falling_back(self):
        with patch.object(credential_store.shutil, "which", return_value=None):
            store = credential_store.CredentialStore(system="Linux")
        self.assertFalse(store.available())
        with self.assertRaises(credential_store.CredentialStoreError) as ctx:
            store.store("work", SECRET)
        self.assertIn("no plaintext fallback", str(ctx.exception))

    def test_keychain_secret_goes_through_stdin_not_argv(self):
        calls = []

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        def runner(argv, stdin_text):
            calls.append((argv, stdin_text))
            return Result()

        with patch.object(credential_store.shutil, "which", return_value="/usr/bin/security"):
            store = credential_store.CredentialStore(system="Darwin", runner=runner)
        store.store("work", SECRET)

        argv, stdin_text = calls[-1]
        self.assertEqual(stdin_text, SECRET)
        self.assertNotIn(SECRET, " ".join(argv))


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.profiles_path = root / "profiles.json"
        self.fake = FakeStore()
        self.patches = [
            patch.object(config, "PROFILES_FILE", self.profiles_path),
            patch.object(api_key_helper, "CredentialStore", lambda: self.fake),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def write_profiles(self, data):
        self.profiles_path.write_text(json.dumps(data), encoding="utf-8")

    def test_prints_only_the_secret_on_stdout(self):
        self.write_profiles({"version": 1, "active": "work", "default": None,
                             "profiles": {"work": {"auth": "api_key", "credential": "work"}}})
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = api_key_helper.main()
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), SECRET)
        self.assertEqual(err.getvalue(), "")

    def test_fails_without_an_active_profile(self):
        self.write_profiles({"version": 1, "active": None, "default": None, "profiles": {}})
        with self.assertRaises(SystemExit):
            api_key_helper.main()


if __name__ == "__main__":
    unittest.main()
