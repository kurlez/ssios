"""OS secret storage for Google OAuth material.

Secrets are namespaced by SSIOS account. The keyring service name is
``ssios.google.<account_id>``. Values go through the ``keyring`` package,
which talks to the macOS Keychain or the Linux Secret Service in-process.
Nothing is written to a file, and no secret is placed on a process command line.
If ``keyring`` is missing or has no backend, the store refuses to open.
"""

from __future__ import annotations

import re
from typing import Protocol

from ssios.errors import SsiosError
from ssios.logsafe import log_failure

SERVICE_PREFIX = "ssios.google."
_PROBE_SERVICE = "ssios.keychain.probe"
_PROBE_NAME = "probe"
_PROBE_VALUE = "ssios-probe"
_KEYCHAIN_HINT = (
    "The macOS login keychain refused this process (error -25308, interaction not allowed). "
    "Unlock it with `security unlock-keychain ~/Library/Keychains/login.keychain-db`, "
    "or run SSIOS from a local GUI login session. "
    "Error code: keychain_locked."
)
SECRET_NAMES = (
    "client-id",
    "client-secret",
    "drive-refresh",
    "gmail-refresh",
    "calendar-refresh",
    "drive-scope",
    "gmail-scope",
    "calendar-scope",
    "drive-connected-at",
    "gmail-connected-at",
    "calendar-connected-at",
    "gmail-reconnect",
    "drive-reconnect",
    "calendar-reconnect",
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
    if re.fullmatch(r"site-[0-9a-f]{16}", name) and isinstance(value, str) and 0 < len(value) <= 4000:
        return
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


def _locked(exc: BaseException) -> bool:
    text = f"{exc.__class__.__name__} {exc}"
    if "-25308" in text or "errSecInteractionNotAllowed" in text or "InteractionNotAllowed" in text:
        return True
    lowered = text.lower()
    return "interaction not allowed" in lowered or "keychain locked" in lowered


def _raise_store(exc: BaseException, action: str) -> None:
    """Map a keyring failure to a stable code. The secret value is not part of the message."""
    log_failure(exc)
    if _locked(exc):
        raise SsiosError("keychain_locked", _KEYCHAIN_HINT, 503) from exc
    raise SsiosError(
        "keychain_error",
        f"The OS secret store could not {action} ({exc.__class__.__name__}). "
        "Error code: keychain_error. Hint: run ./scripts/ssios diagnose.",
        503,
    ) from exc


class KeyringSecretStore:
    def __init__(self, module) -> None:
        self._keyring = module

    def put(self, account_id: str, name: str, value: str) -> None:
        _accept(name, value)
        try:
            self._keyring.set_password(service_name(account_id), name, value)
        except SsiosError:
            raise
        except Exception as exc:
            _raise_store(exc, "store a secret")

    def get(self, account_id: str, name: str) -> str | None:
        _accept(name, "x")
        try:
            return self._keyring.get_password(service_name(account_id), name)
        except SsiosError:
            raise
        except Exception as exc:
            _raise_store(exc, "read a secret")

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
        except SsiosError:
            raise
        except Exception as exc:
            if _locked(exc):
                _raise_store(exc, "delete a secret")
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


def probe_keychain(module=None) -> dict:
    """Write, read, and delete one non-secret item. Nothing is written to a file."""
    if module is None:
        try:
            import keyring
        except ImportError:
            return {"ok": False, "code": "secure_store_unavailable", "detail": _INSTALL}
        module = keyring
    backend = module.get_keyring()
    if backend.__class__.__name__ == "FailKeyring":
        return {"ok": False, "code": "secure_store_unavailable", "detail": _INSTALL}
    try:
        module.set_password(_PROBE_SERVICE, _PROBE_NAME, _PROBE_VALUE)
        found = module.get_password(_PROBE_SERVICE, _PROBE_NAME)
        if found != _PROBE_VALUE:
            return {
                "ok": False,
                "code": "keychain_error",
                "detail": "The keychain probe could not read the test item back. Error code: keychain_error.",
            }
        return {"ok": True, "code": "ok", "detail": "The keychain accepted a probe item and it was deleted."}
    except Exception as exc:
        if _locked(exc):
            return {"ok": False, "code": "keychain_locked", "detail": _KEYCHAIN_HINT}
        log_failure(exc)
        return {
            "ok": False,
            "code": "keychain_error",
            "detail": (
                f"The keychain probe failed ({exc.__class__.__name__}). "
                "Error code: keychain_error. Hint: run ./scripts/ssios diagnose."
            ),
        }
    finally:
        try:
            module.delete_password(_PROBE_SERVICE, _PROBE_NAME)
        except Exception:
            pass
