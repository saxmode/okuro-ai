# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Secure storage for secrets with Fernet encryption at rest.
# index:
#   imports
#   def real_user_home
#   def _get_from_os_keyring
#   def _set_in_os_keyring
#   def _delete_from_os_keyring
#   def _read_machine_id
#   def _read_or_create_machine_fallback
#   def _resolve_machine_id
#   def _derive_fernet_key_machine_bound
#   class KeyringStorage
# AGENT_HEADER_END -->
"""Secure storage for secrets with Fernet encryption at rest.

Master password resolution order:
1. Explicit password passed to constructor
2. OKURO_KEYRING_PASSWORD env var (headless/CI/Docker)
3. OS credential store (GNOME Keyring / macOS Keychain / Windows Credential Locker)
4. TM_KEYRING_PASSWORD env var (backward compat, will be removed)
5. Machine-bound HKDF-derived key (file fallback when no OS keyring is available)

Threat-model trade-off (file fallback):
    When the OS keyring is unavailable we no longer write the master password
    to ``~/.okuro/keyring/config.json`` in plaintext (audit finding #19,
    sprint 1E). Instead we derive the Fernet key with HKDF-SHA256 from the
    host machine-id (`/etc/machine-id`, `/var/lib/dbus/machine-id`, the
    macOS IOPlatformUUID, or a one-time random fallback file at
    ``~/.okuro/keyring/.machine-fallback``) plus a 32-byte random salt
    persisted in ``config.json``.

    Consequence — the vault is BOUND to the host. A backup of
    ``~/.okuro/keyring/`` restored to a different machine WILL fail to
    decrypt with a clear "bound to a different machine" error. This is
    intentional: a single-file leak (umask race, root, backup dump) no
    longer exposes the vault on its own — the attacker also needs the
    target machine's machine-id. To migrate to a new machine: copy the
    plaintext secrets via ``okuro keys export`` on the original host and
    re-import via ``okuro keys init`` + import on the new host.
"""

import base64
import json
import logging
import os
import platform
import secrets as secrets_mod
import stat
import subprocess
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

_OS_KEYRING_SERVICE = "okuro"
_OS_KEYRING_USERNAME = "master"

_HKDF_INFO = b"okuro-keyring-v1"
_HKDF_SCHEME = "hkdf-sha256-machine-id-v1"
_MACHINE_FALLBACK_FILENAME = ".machine-fallback"
_RECOVERY_HINT = (
    "Keyring is bound to a different machine. To recover, re-run "
    "`okuro keys init` (existing secrets will be lost) or restore the "
    "original `~/.okuro/keyring/` from a backup made on the original machine."
)

log = logging.getLogger(__name__)


def real_user_home() -> Path:
    """Return the real user home, ignoring `$HOME` overrides.

    Subagents may run under a scoped HOME (`~/.okuro-agent-home/`) so that
    their MCP tool configs are isolated. But the encrypted keyring vault is
    per-UID, not per-process — resolving via `Path.home()` would send the
    subagent looking inside `~/.okuro-agent-home/.okuro/keyring/` where
    nothing exists. This helper ignores `$HOME` and reads the real home from
    the OS user database (POSIX) or `%USERPROFILE%` (Windows).
    """
    if platform.system() == "Windows":
        profile = os.environ.get("USERPROFILE")
        if profile:
            return Path(profile)
        return Path.home()
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, ImportError):
        return Path.home()


def _get_from_os_keyring() -> Optional[str]:
    """Retrieve master password from OS credential store."""
    try:
        import keyring as kr
        return kr.get_password(_OS_KEYRING_SERVICE, _OS_KEYRING_USERNAME)
    except Exception:
        return None


def _set_in_os_keyring(password: str) -> bool:
    """Store master password in OS credential store."""
    try:
        import keyring as kr
        kr.set_password(_OS_KEYRING_SERVICE, _OS_KEYRING_USERNAME, password)
        return True
    except Exception:
        return False


def _delete_from_os_keyring() -> bool:
    """Remove master password from OS credential store."""
    try:
        import keyring as kr
        kr.delete_password(_OS_KEYRING_SERVICE, _OS_KEYRING_USERNAME)
        return True
    except Exception:
        return False


def _read_machine_id() -> Optional[bytes]:
    """Read machine-id from OS-managed locations.

    Linux: /etc/machine-id, then /var/lib/dbus/machine-id.
    macOS: ioreg IOPlatformUUID.
    Returns the raw bytes, or None if unavailable.
    """
    system = platform.system()
    if system == "Linux":
        for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                value = Path(candidate).read_text().strip()
                if value:
                    return value.encode()
            except (OSError, PermissionError):
                continue
        return None
    if system == "Darwin":
        try:
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if out.returncode == 0:
                for line in out.stdout.splitlines():
                    if "IOPlatformUUID" in line:
                        # Line looks like: "IOPlatformUUID" = "ABCD-1234..."
                        parts = line.split('"')
                        if len(parts) >= 4 and parts[-2].strip():
                            return parts[-2].strip().encode()
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
        return None
    # Windows / unknown — no first-class machine-id source we trust.
    # Fallback file path will be used.
    return None


def _read_or_create_machine_fallback(keyring_dir: Path) -> bytes:
    """Read or create a 32-byte random fallback machine-id file (0600).

    Used when no OS-managed machine-id is available. The file lives inside
    the keyring dir so it shares the same per-user permission boundary.
    """
    fallback = keyring_dir / _MACHINE_FALLBACK_FILENAME
    if fallback.exists():
        return fallback.read_bytes()
    keyring_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(keyring_dir, stat.S_IRWXU)
    data = secrets_mod.token_bytes(32)
    fallback.write_bytes(data)
    os.chmod(fallback, stat.S_IRUSR | stat.S_IWUSR)
    return data


def _resolve_machine_id(keyring_dir: Path) -> bytes:
    """Resolve machine-id IKM; fall through to the random fallback file."""
    machine_id = _read_machine_id()
    if machine_id:
        return machine_id
    return _read_or_create_machine_fallback(keyring_dir)


def _derive_fernet_key_machine_bound(machine_id: bytes, salt: bytes) -> bytes:
    """Derive a 32-byte Fernet key (urlsafe-b64-encoded) via HKDF-SHA256."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=_HKDF_INFO,
    )
    return base64.urlsafe_b64encode(hkdf.derive(machine_id))


class KeyringStorage:
    """Encrypted secrets vault.

    Keys are stored in a directory with:
    - keys.enc: Encrypted JSON blob of all secrets
    - salt: 32-byte random salt for PBKDF2 key derivation (master-password mode)
    - config.json: Either {} (OS-keyring mode) or
                   {"version": 1, "scheme": "hkdf-sha256-machine-id-v1",
                    "salt": "<b64>"} (machine-bound file-fallback mode).
                   Legacy {"password": "..."} configs are migrated on load.
    - .machine-fallback: 32-byte random IKM file used when no OS machine-id
                        is available (created lazily, 0600).
    - .initialized: Marker file

    Master password is resolved from OS credential store automatically.
    Falls back to OKURO_KEYRING_PASSWORD env var for headless environments.
    When neither is available, the Fernet key is derived from the host
    machine-id via HKDF (see module docstring for the threat-model
    trade-off — vault is bound to the host).
    """

    def __init__(
        self,
        master_password: Optional[str] = None,
        keyring_dir: Optional[Path] = None,
    ):
        # Use real_user_home() so the vault is reachable even when a
        # subagent is running under a scoped HOME (see Gap 4 in packaging audit).
        self._dir = keyring_dir or real_user_home() / ".okuro" / "keyring"
        self._master_password = (
            master_password
            or os.environ.get("OKURO_KEYRING_PASSWORD")
            or _get_from_os_keyring()
            or os.environ.get("TM_KEYRING_PASSWORD")  # backward compat
        )
        self._fernet: Optional[Fernet] = None
        self._keys: dict[str, str] = {}
        self._keys_loaded: bool = False
        self._keys_mtime: Optional[float] = None

    @property
    def keys_file(self) -> Path:
        return self._dir / "keys.enc"

    @property
    def salt_file(self) -> Path:
        return self._dir / "salt"

    @property
    def config_file(self) -> Path:
        return self._dir / "config.json"

    @property
    def machine_fallback_file(self) -> Path:
        return self._dir / _MACHINE_FALLBACK_FILENAME

    @property
    def is_initialized(self) -> bool:
        return (self._dir / ".initialized").exists() and self.salt_file.exists()

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self._dir, stat.S_IRWXU)

    def _derive_key(self, password: str, salt: bytes) -> bytes:
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=480000,
        )
        return base64.urlsafe_b64encode(kdf.derive(password.encode()))

    # ---- machine-bound (file fallback) helpers ---------------------------

    def _read_config(self) -> dict:
        if not self.config_file.exists():
            return {}
        try:
            return json.loads(self.config_file.read_text())
        except (OSError, ValueError):
            return {}

    def _write_machine_bound_config(self, salt: bytes) -> None:
        """Write the salt + scheme metadata. NEVER includes the password."""
        self._ensure_dir()
        payload = {
            "version": 1,
            "scheme": _HKDF_SCHEME,
            "salt": base64.b64encode(salt).decode("ascii"),
        }
        # Atomic-ish: write then chmod. config.json must be 0600 because
        # while it no longer leaks the key on its own, it pins the salt.
        self.config_file.write_text(json.dumps(payload))
        os.chmod(self.config_file, stat.S_IRUSR | stat.S_IWUSR)

    def _machine_bound_fernet(self, salt: bytes) -> Fernet:
        machine_id = _resolve_machine_id(self._dir)
        key = _derive_fernet_key_machine_bound(machine_id, salt)
        return Fernet(key)

    def _try_machine_bound_decrypt(self, config: dict) -> Optional[Fernet]:
        """Return a Fernet built from the machine-bound key, or None if
        config has no HKDF metadata. Raises ValueError with a recovery
        hint if metadata is present but decryption of the existing
        keys.enc fails (machine-id rotated → vault unreadable).
        """
        if config.get("scheme") != _HKDF_SCHEME or "salt" not in config:
            return None
        try:
            salt = base64.b64decode(config["salt"])
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Corrupt keyring config.json: {exc}")
        fernet = self._machine_bound_fernet(salt)
        # Probe-decrypt against existing keys.enc if present so machine-id
        # rotation surfaces immediately with the recovery hint.
        if self.keys_file.exists():
            try:
                fernet.decrypt(self.keys_file.read_bytes())
            except Exception as exc:
                raise ValueError(_RECOVERY_HINT) from exc
        return fernet

    def _migrate_legacy_password_config(self, config: dict) -> Optional[Fernet]:
        """Detect legacy {"password": ...} config.json and re-encrypt.

        The legacy plaintext password is used to decrypt keys.enc with the
        existing PBKDF2 salt; the vault is then re-encrypted with a fresh
        HKDF-derived machine-bound key, and config.json is rewritten WITHOUT
        the password. Returns the new Fernet, or None if not a legacy config.
        """
        if "password" not in config:
            return None
        legacy_password = config["password"]
        if not self.salt_file.exists():
            # Legacy config but no salt — nothing to migrate from. Treat as
            # uninitialized; let normal init flow create new state.
            return None
        legacy_salt = self.salt_file.read_bytes()
        legacy_fernet = Fernet(self._derive_key(legacy_password, legacy_salt))

        # Decrypt keys.enc (if any) with legacy key, re-encrypt with new key.
        existing_keys: dict[str, str] = {}
        if self.keys_file.exists():
            try:
                existing_keys = json.loads(
                    legacy_fernet.decrypt(self.keys_file.read_bytes()).decode()
                )
            except Exception as exc:
                raise ValueError(
                    f"Legacy config.json present but vault decryption failed: {exc}"
                )

        new_salt = secrets_mod.token_bytes(32)
        new_fernet = self._machine_bound_fernet(new_salt)
        # Re-encrypt vault with new key, then swap config.json.
        encrypted = new_fernet.encrypt(json.dumps(existing_keys, indent=2).encode())
        self.keys_file.write_bytes(encrypted)
        os.chmod(self.keys_file, stat.S_IRUSR | stat.S_IWUSR)
        self._write_machine_bound_config(new_salt)
        log.info(
            "Migrated keyring config.json from legacy plaintext-password to "
            "machine-bound HKDF (scheme=%s)",
            _HKDF_SCHEME,
        )
        return new_fernet

    # ---- main key-resolution path ----------------------------------------

    def _get_fernet(self) -> Fernet:
        if self._fernet is not None:
            return self._fernet

        config = self._read_config()

        # 1. Legacy plaintext-password config → migrate to machine-bound HKDF.
        migrated = self._migrate_legacy_password_config(config)
        if migrated is not None:
            self._fernet = migrated
            return self._fernet

        # 2. Machine-bound HKDF config (current scheme).
        machine_bound = self._try_machine_bound_decrypt(config)
        if machine_bound is not None:
            self._fernet = machine_bound
            return self._fernet

        # 3. Master-password mode (OS keyring or env var supplied a secret).
        if not self._master_password:
            raise ValueError(
                "Master password not found. Run 'okuro keys init' to set up the keyring, "
                "or set OKURO_KEYRING_PASSWORD env var for headless environments."
            )
        if not self.salt_file.exists():
            raise ValueError("Keyring not initialized. Run 'okuro keys init' first.")
        salt = self.salt_file.read_bytes()
        self._fernet = Fernet(self._derive_key(self._master_password, salt))
        return self._fernet

    def initialize(self, password: str) -> None:
        """Initialize the keyring with a master password.

        Stores the password in the OS credential store (GNOME Keyring,
        macOS Keychain, or Windows Credential Locker). Falls back to a
        machine-bound HKDF-derived key (NO plaintext password on disk) for
        headless environments where no OS keyring is available.
        """
        self._ensure_dir()
        salt = secrets_mod.token_bytes(32)
        self.salt_file.write_bytes(salt)
        os.chmod(self.salt_file, stat.S_IRUSR | stat.S_IWUSR)

        self._master_password = password
        self._fernet = None
        self._keys = {}

        # Decide the storage mode BEFORE saving keys, so _save_keys uses
        # the correct Fernet from the start.
        if _set_in_os_keyring(password):
            # OS keyring path — drop any leftover config.json (legacy or HKDF).
            if self.config_file.exists():
                self.config_file.unlink()
            self._save_keys()
        else:
            # Headless fallback: derive a machine-bound HKDF key. The user-
            # supplied `password` is intentionally not persisted anywhere —
            # the host's machine-id replaces it. config.json holds only the
            # HKDF salt + scheme metadata.
            hkdf_salt = secrets_mod.token_bytes(32)
            self._write_machine_bound_config(hkdf_salt)
            self._fernet = self._machine_bound_fernet(hkdf_salt)
            # Reset master_password so subsequent _get_fernet() doesn't
            # accidentally fall back to PBKDF2 mode if config.json is wiped.
            self._master_password = None
            self._save_keys()

        marker = self._dir / ".initialized"
        marker.touch()
        os.chmod(marker, stat.S_IRUSR | stat.S_IWUSR)

    def _load_keys(self) -> None:
        if not self.keys_file.exists():
            self._keys = {}
            self._keys_loaded = True
            self._keys_mtime = None
            return
        # Cache: skip re-decrypt if file mtime unchanged since last load.
        # External writes (other processes) change mtime and invalidate cache.
        mtime = self.keys_file.stat().st_mtime
        if self._keys_loaded and self._keys_mtime == mtime:
            return
        fernet = self._get_fernet()
        encrypted = self.keys_file.read_bytes()
        try:
            self._keys = json.loads(fernet.decrypt(encrypted).decode())
            self._keys_loaded = True
            self._keys_mtime = mtime
        except Exception as e:
            raise ValueError(f"Decryption failed — wrong password? {e}")

    def _save_keys(self) -> None:
        self._ensure_dir()
        fernet = self._get_fernet()
        data = json.dumps(self._keys, indent=2).encode()
        self.keys_file.write_bytes(fernet.encrypt(data))
        os.chmod(self.keys_file, stat.S_IRUSR | stat.S_IWUSR)
        self._keys_mtime = self.keys_file.stat().st_mtime
        self._keys_loaded = True

    def add_key(self, name: str, value: str) -> None:
        self._load_keys()
        self._keys[name] = value
        self._save_keys()

    # Alias — some callers use set_key (idempotent overwrite semantics).
    set_key = add_key

    def get_key(self, name: str) -> Optional[str]:
        self._load_keys()
        return self._keys.get(name)

    def delete_key(self, name: str) -> bool:
        self._load_keys()
        if name in self._keys:
            del self._keys[name]
            self._save_keys()
            return True
        return False

    def list_keys(self) -> list[str]:
        self._load_keys()
        return sorted(self._keys.keys())

    def get_all_keys(self) -> dict[str, str]:
        self._load_keys()
        return dict(self._keys)

    def import_keys(self, keys: dict[str, str]) -> int:
        self._load_keys()
        count = 0
        for name, value in keys.items():
            if value and value.strip():
                self._keys[name] = value.strip()
                count += 1
        self._save_keys()
        return count
