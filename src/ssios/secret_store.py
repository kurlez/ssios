"""OS secret storage for Google OAuth material.

Secrets are namespaced by SSIOS account. The keyring service name is
``ssios.google.<account_id>``. Values go through the ``keyring`` package,
which talks to the macOS Keychain or the Linux Secret Service in-process.
Nothing is written to a file, and no secret is placed on a process command line.
If ``keyring`` is missing or has no backend, the store refuses to open.
"""

from __future__ import annotations

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
_INSTALL = "OS secret storage needs the keyring package. Run: pip install keyring"


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


def _accept(name: str, value: str) -> None:
    if name not in SECRET_NAMES or not isinstance(value, str) or not value:
        raise SsiosError("secret_rejected", "secret name rejected", 400)


class MemorySecretStore:
    """In-memory stand-in for tests. Production never selects this store."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def put(self, account_id: str, name: str, value: str) -> None:
        _accept(name, value)
        self.data[(service_name(account_id), name)] = value

    def get(self, account_id: str, name: str) -> str | None:
        _accept(name, "x")
        return self.data.get((service_name(account_id), name))

    def delete(self, account_id: str, name: str) -> None:
        _accept(name, "x")
        self.data.pop((service_name(account_id), name), None)

    def delete_service(self, account_id: str) -> None:
        service = service_name(account_id)
        for key in [key for key in self.data if key[0] == service]:
            self.data.pop(key, None)


class KeyringSecretStore:
    def __init__(self, module) -> None:
        self._keyring = module

    def put(self, account_id: str, name: str, value: str) -> None:
        _accept(name, value)
        self._keyring.set_password(service_name(account_id), name, value)

    def get(self, account_id: str, name: str) -> str | None:
        _accept(name, "x")
        return self._keyring.get_password(service_name(account_id), name)

    def delete(self, account_id: str, name: str) -> None:
        _accept(name, "x")
        self._delete(service_name(account_id), name)

    def delete_service(self, account_id: str) -> None:
        service = service_name(account_id)
        for name in SECRET_NAMES:
            self._delete(service, name)

    def _delete(self, service: str, name: str) -> None:
        try:
            self._keyring.delete_password(service, name)
        except Exception:
            return


def open_secret_store() -> SecretStore:
    """Open the OS store. Refuses when keyring is missing instead of using a CLI or a file."""
    try:
        import keyring
    except ImportError as exc:
        raise SsiosError("secure_store_unavailable", _INSTALL, 500) from exc
    backend = keyring.get_keyring()
    if backend.__class__.__name__ == "FailKeyring":
        raise SsiosError("secure_store_unavailable", _INSTALL, 500)
    return KeyringSecretStore(keyring)
