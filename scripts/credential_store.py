"""OS-backed credential storage.

Secrets never touch disk in plaintext: every backend writes into an
OS-managed store (Keychain, Secret Service, Windows Credential Manager,
or a DPAPI user-scoped blob). When no backend is usable we refuse - there
is deliberately no plaintext fallback.

The secret is passed to backends on stdin, never on a command line.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

SERVICE = "provider-switch"
DATA_DIR = Path.home() / ".claude" / "plugins" / "data" / "provider-switch"
CRED_DIR = DATA_DIR / "credentials"

# Windows PowerShell 5.1 can fail to auto-load its built-in modules when the
# parent process injects a PS7-flavoured PSModulePath ($PSHOME\Modules ends up
# last, so 5.1 resolves `Microsoft.PowerShell.Security` to a PS7-only copy).
# Fix: resolve Security by explicit $PSHOME path inline, and (for env-driven
# spawns) restore the 5.1 module dirs.
_PS_SECURITY_IMPORT = (
    "Import-Module (Join-Path $PSHOME "
    "'Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1') "
    "-ErrorAction Stop;"
)

UNAVAILABLE_MESSAGE = (
    "No secure credential store is available on this machine.\n"
    "Refusing to write the key anywhere else - there is no plaintext fallback.\n"
    "Fix one of these, then retry:\n"
    "  Linux:   install `secret-tool` (libsecret) and unlock your keyring\n"
    "  macOS:   ensure the `security` command is on PATH\n"
    "  Windows: ensure PowerShell is available"
)


class CredentialStoreError(RuntimeError):
    """Raised when a credential cannot be stored or read securely."""


class CredentialStore:
    """Dispatch to a platform backend. `runner` is injectable for tests."""

    def __init__(self, system=None, runner=None, run_env=None):
        self.system = (system or platform.system()).lower()
        self._run = runner or self._default_run
        self._run_env = run_env or self._default_run_env
        self.backend = self._select_backend()

    # ---- backend selection -------------------------------------------------

    def _select_backend(self):
        if self.system == "darwin":
            return "keychain" if shutil.which("security") else "unavailable"
        if self.system == "windows":
            if not shutil.which("powershell") and not shutil.which("pwsh"):
                return "unavailable"
            if self._powershell_has_secretmanagement():
                return "secretmanagement"
            return "dpapi"
        if shutil.which("secret-tool"):
            return "secretservice"
        return "unavailable"

    def _powershell_has_secretmanagement(self):
        try:
            out = self._run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "if (Get-Command Get-Secret -ErrorAction SilentlyContinue) { 'yes' }"],
                None,
            )
            return "yes" in (out.stdout or "")
        except Exception:
            return False

    def _default_run(self, argv, stdin_text):
        return subprocess.run(
            argv, input=stdin_text, text=True, capture_output=True, timeout=60
        )

    def _default_run_env(self, argv, env):
        return subprocess.run(argv, text=True, capture_output=True, timeout=60, env=env)

    # ---- public API --------------------------------------------------------

    def available(self):
        return self.backend != "unavailable"

    def describe(self):
        return {
            "keychain": "macOS Keychain",
            "secretservice": "Secret Service (secret-tool)",
            "secretmanagement": "Windows Credential Manager (SecretManagement)",
            "dpapi": "Windows DPAPI, user-scoped blob",
            "unavailable": "no secure store available",
        }[self.backend]

    def store(self, ref, secret):
        self._require(secret)
        getattr(self, "_store_" + self.backend)(ref, secret)

    def retrieve(self, ref):
        return getattr(self, "_get_" + self.backend)(ref)

    def delete(self, ref):
        return getattr(self, "_del_" + self.backend)(ref)

    def _require(self, secret):
        if not self.available():
            raise CredentialStoreError(UNAVAILABLE_MESSAGE)
        if not secret:
            raise CredentialStoreError("Refusing to store an empty secret.")

    # ---- macOS Keychain ----------------------------------------------------

    def _store_keychain(self, ref, secret):
        # -U updates in place; the secret arrives on stdin, not argv.
        r = self._run(
            ["security", "add-generic-password", "-s", SERVICE, "-a", ref, "-U", "-w"],
            secret,
        )
        if r.returncode != 0:
            raise CredentialStoreError("Keychain write failed: " + _tail(r.stderr))

    def _get_keychain(self, ref):
        r = self._run(
            ["security", "find-generic-password", "-s", SERVICE, "-a", ref, "-w"], None
        )
        if r.returncode != 0:
            raise CredentialStoreError("No keychain entry for %r." % ref)
        return (r.stdout or "").rstrip("\n")

    def _del_keychain(self, ref):
        r = self._run(
            ["security", "delete-generic-password", "-s", SERVICE, "-a", ref], None
        )
        return r.returncode == 0

    # ---- Linux Secret Service ---------------------------------------------

    def _store_secretservice(self, ref, secret):
        r = self._run(
            ["secret-tool", "store", "--label", SERVICE + ":" + ref,
             "service", SERVICE, "account", ref],
            secret,
        )
        if r.returncode != 0:
            raise CredentialStoreError("secret-tool write failed: " + _tail(r.stderr))

    def _get_secretservice(self, ref):
        r = self._run(["secret-tool", "lookup", "service", SERVICE, "account", ref], None)
        if r.returncode != 0 or not r.stdout:
            raise CredentialStoreError("No Secret Service entry for %r." % ref)
        return r.stdout.rstrip("\n")

    def _del_secretservice(self, ref):
        r = self._run(["secret-tool", "clear", "service", SERVICE, "account", ref], None)
        return r.returncode == 0

    # ---- Windows SecretManagement -----------------------------------------

    def _store_secretmanagement(self, ref, secret):
        script = (
            "$ErrorActionPreference='Stop';"
            + _PS_SECURITY_IMPORT
            + "$s=[Console]::In.ReadToEnd().TrimEnd();"
            "Set-Secret -Name '" + _ps(ref) + "' "
            "-Secret (ConvertTo-SecureString -String $s -AsPlainText -Force)"
        )
        r = self._run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], secret)
        if r.returncode != 0:
            raise CredentialStoreError("Set-Secret failed: " + _tail(r.stderr))

    def _get_secretmanagement(self, ref):
        script = (
            "$ErrorActionPreference='Stop';"
            + _PS_SECURITY_IMPORT
            + "$s=Get-Secret -Name '" + _ps(ref) + "' -AsPlainText;"
            "[Console]::Out.Write($s)"
        )
        r = self._run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], None)
        if r.returncode != 0 or not (r.stdout or ""):
            raise CredentialStoreError("No SecretManagement entry for %r." % ref)
        return r.stdout

    def _del_secretmanagement(self, ref):
        script = (
            "$ErrorActionPreference='SilentlyContinue';"
            "Remove-Secret -Name '" + _ps(ref) + "'; exit 0"
        )
        r = self._run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], None)
        return r.returncode == 0

    # ---- Windows DPAPI blob ------------------------------------------------

    def _dpapi_path(self, ref):
        return CRED_DIR / (_slug(ref) + ".dpapi")

    def _store_dpapi(self, ref, secret):
        CRED_DIR.mkdir(parents=True, exist_ok=True)
        path = self._dpapi_path(ref)
        script = (
            "$ErrorActionPreference='Stop';"
            + _PS_SECURITY_IMPORT
            + "$s=[Console]::In.ReadToEnd().TrimEnd();"
            "ConvertFrom-SecureString -SecureString "
            "(ConvertTo-SecureString -String $s -AsPlainText -Force)"
        )
        r = self._run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], secret)
        if r.returncode != 0:
            raise CredentialStoreError("DPAPI encrypt failed: " + _tail(r.stderr))
        path.write_text(r.stdout.strip(), encoding="utf-8")
        _restrict(path)

    def _get_dpapi(self, ref):
        path = self._dpapi_path(ref)
        if not path.exists():
            raise CredentialStoreError("No stored credential for %r." % ref)
        script = (
            "$ErrorActionPreference='Stop';"
            + _PS_SECURITY_IMPORT
            + "$e=Get-Content -Raw -LiteralPath $env:PS_CRED_FILE;"
            "$ss=ConvertTo-SecureString $e;"
            "[Console]::Out.Write((New-Object System.Net.NetworkCredential('', $ss)).Password)"
        )
        env = dict(os.environ, PS_CRED_FILE=str(path))
        env["PSModulePath"] = _ps51_module_path()
        r = self._run_env(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env)
        if r.returncode != 0 or not (r.stdout or ""):
            raise CredentialStoreError("Could not decrypt stored credential for %r." % ref)
        return r.stdout

    def _del_dpapi(self, ref):
        path = self._dpapi_path(ref)
        if path.exists():
            path.unlink()
            return True
        return False

    # ---- unavailable -------------------------------------------------------

    def _store_unavailable(self, ref, secret):
        raise CredentialStoreError(UNAVAILABLE_MESSAGE)

    def _get_unavailable(self, ref):
        raise CredentialStoreError("No secure credential store is available.")

    def _del_unavailable(self, ref):
        return False


def _ps51_module_path():
    # Explicit 5.1 module dirs (same order `powershell` uses natively) so the
    # child resolves its own built-ins regardless of inherited PSModulePath.
    windir = os.environ.get("WINDIR", "C:\\Windows")
    progfiles = os.environ.get("ProgramFiles", "C:\\Program Files")
    return os.pathsep.join([
        os.path.join(windir, "System32", "WindowsPowerShell", "v1.0", "Modules"),
        os.path.join(progfiles, "WindowsPowerShell", "Modules"),
    ])


def _tail(stderr):
    lines = (stderr or "").strip().splitlines()
    return lines[-1] if lines else "unknown error"


def _ps(ref):
    """Escape a reference for a single-quoted PowerShell string."""
    return ref.replace("'", "''")


def _slug(ref):
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in ref)


def _restrict(path):
    """Best-effort owner-only permissions."""
    try:
        if platform.system().lower() == "windows":
            user = os.environ.get("USERNAME")
            if user:
                subprocess.run(
                    ["icacls", str(path), "/inheritance:r", "/grant:r", user + ":F"],
                    capture_output=True, timeout=30,
                )
        else:
            path.chmod(0o600)
    except Exception:
        pass
