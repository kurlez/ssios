"""OS secret storage for Google OAuth material.

Secrets are namespaced by SSIOS account: the Keychain / Secret Service
service name is ``ssios.google.<account_id>``. This module never writes a
secret to a file. Tests inject ``MemorySecretStore``; production refuses to
start a store when neither keyring, macOS ``security``, nor ``secret-tool``
is available.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Protocol

from ssios.errors import SsiosError

SERVICE_PREFIX = "ssios.google."
SECRET_NAMES = (
    "client-id",
    "client-secret",
    "drive-refresh",
    "gmail-refresh",
    "drive-scope",
    "gmail-scope",
    "google-sub",
    "connected-at",
)


def validate_account_id(account_id: str) -> str:
    if not isinstance(account_id, str) or not account_id:
        raise SsiosError("account_invalid", "account id rejected", 400)
    if len(account_id) > 64 or not account_id[0].isalnum():
        raise SsiosError("account_invalid", "account id rejected", 400)
    for char in account_id:
        if not (char.isalnum() or char in "-_"):
            raise SsiosError("account_invalid", "account id rejected", 400)
    return account_id


def service_name(account_id: str) -> str:
    return SERVICE_PREFIX + validate_account_id(account_id)


class SecretStore(Protocol):
    def put(self, account_id: str, name: str, value: str) -> None: ...

    def get(self, account_id: str, name: str) -> str | None: ...

    def delete(self, account_id: str, name: str) -> None: ...

    def delete_service(self, account_id: str) -> None: ...


class MemorySecretStore:
    """In-memory stand-in for tests. Not used in production."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def put(self, account_id: str, name: str, value: str) -> None:
        self._check(name, value)
        self.data[(service_name(account_id), name)] = value

    def get(self, account_id: str, name: str) -> str | None:
        self._check(name, "x")
        return self.data.get((service_name(account_id), name))

    def delete(self, account_id: str, name: str) -> None:
        self._check(name, "x")
        self.data.pop((service_name(account_id), name), None)

    def delete_service(self, account_id: str) -> None:
        service = service_name(account_id)
        for key in [key for key in self.data if key[0] == service]:
            self.data.pop(key, None)

    @staticmethod
    def _check(name: str, value: str) -> None:
        if name not in SECRET_NAMES or not isinstance(value, str) or not value:
            raise SsiosError("secret_rejected", "secret name rejected", 400)


class _CommandStore:
    def put(self, account_id: str, name: str, value: str) -> None:
        if name not in SECRET_NAMES or not value:
            raise SsiosError("secret_rejected", "secret name rejected", 400)
        self._put(service_name(account_id), name, value)

    def get(self, account_id: str, name: str) -> str | None:
        if name not in SECRET_NAMES:
            raise SsiosError("secret_rejected", "secret name rejected", 400)
        return self._get(service_name(account_id), name)

    def delete(self, account_id: str, name: str) -> None:
        if name not in SECRET_NAMES:
            raise SsiosError("secret_rejected", "secret name rejected", 400)
        self._delete(service_name(account_id), name)

    def delete_service(self, account_id: str) -> None:
        service = service_name(account_id)
        for name in SECRET_NAMES:
            self._delete(service, name)

    def _put(self, service: str, name: str, value: str) -> None:
        raise NotImplementedError

    def _get(self, service: str, name: str) -> str | None:
        raise NotImplementedError

    def _delete(self, service: str, name: str) -> None:
        raise NotImplementedError


class KeyringSecretStore(_CommandStore):
    def __init__(self, module) -> None:
        self._keyring = module

    def _put(self, service: str, name: str, value: str) -> None:
        self._keyring.set_password(service, name, value)

    def _get(self, service: str, name: str) -> str | None:
        return self._keyring.get_password(service, name)

    def _delete(self, service: str, name: str) -> None:
        try:
            self._keyring.delete_password(service, name)
        except Exception:
            return


class SecurityCliStore(_CommandStore):
    """macOS Keychain via the security CLI. The secret is not written to a file."""

    def _put(self, service: str, name: str, value: str) -> None:
        completed = subprocess.run(
            ["security", "add-generic-password", "-U", "-s", service, "-a", name, "-w", value],
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise SsiosError("secure_storage_unavailable", "keychain write failed", 500)

    def _get(self, service: str, name: str) -> str | None:
        completed = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", name, "-w"],
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            return None
        text = completed.stdout.decode("utf-8", "replace").strip()
        return text or None

    def _delete(self, service: str, name: str) -> None:
        subprocess.run(
            ["security", "delete-generic-password", "-s", service, "-a", name],
            capture_output=True,
            check=False,
        )


class SecretToolStore(_CommandStore):
    """Linux Secret Service via secret-tool. The secret is passed on stdin."""

    def _put(self, service: str, name: str, value: str) -> None:
        completed = subprocess.run(
            ["secret-tool", "store", "--label=ssios", "service", service, "username", name],
            input=value.encode("utf-8"),
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise SsiosError("secure_storage_unavailable", "secret service write failed", 500)

    def _get(self, service: str, name: str) -> str | None:
        completed = subprocess.run(
            ["secret-tool", "lookup", "service", service, "username", name],
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            return None
        text = completed.stdout.decode("utf-8", "replace")
        return text or None

    def _delete(self, service: str, name: str) -> None:
        subprocess.run(
            ["secret-tool", "clear", "service", service, "username", name],
            capture_output=True,
            check=False,
        )


def _keyring_store() -> SecretStore | None:
    try:
        import keyring
    except ImportError:
        return None
    backend = keyring.get_keyring()
    if backend.__class__.__name__ == "FailKeyring":
        return None
    return KeyringSecretStore(keyring)


def open_secret_store() -> SecretStore:
    """Open the OS store. Raises if that would require a plaintext file."""
    store = _keyring_store()
    if store is not None:
        return store
    if sys.platform == "darwin" and shutil.which("security"):
        return SecurityCliStore()
    if shutil.which("secret-tool"):
        return SecretToolStore()
    raise SsiosError(
        "secure_storage_unavailable",
        "OS secret storage is required (macOS Keychain or Linux Secret Service). Refusing to write a token file.",
        500,
    )
