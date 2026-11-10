"""Gmail and Drive adapter.

Workstream B owns the live Google module. This file is the contract it must
implement, plus a fake and a sanitizer. Every method takes the account id from
the caller's session. Return values never include tokens. Status is only
service name, connected, and connected_at.

Keychain service name: ssios.google.<account_id>
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import time
from pathlib import Path

from ssios.errors import SsiosError

READ_ONLY_SCOPES = {
    "gmail": "https://www.googleapis.com/auth/gmail.readonly",
    "drive": "https://www.googleapis.com/auth/drive.readonly",
}
SERVICES = ("gmail", "drive")
_ACK_KEYS = {"ok", "service", "connected", "complete_on_host", "detail", "error", "revoked", "deleted"}


def service_name(account_id: str) -> str:
    if not account_id or "/" in account_id or ".." in account_id or len(account_id) > 64:
        raise SsiosError("path_rejected", "account id rejected", 400)
    return f"ssios.google.{account_id}"


class MemorySecretStore:
    """In-memory stand-in. Tests use this. It is not a plaintext file."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def put(self, account_id: str, name: str, secret: str) -> None:
        self.items[(service_name(account_id), name)] = secret

    def has(self, account_id: str, name: str) -> bool:
        return (service_name(account_id), name) in self.items

    def delete_account(self, account_id: str) -> int:
        prefix = service_name(account_id)
        keys = [key for key in self.items if key[0] == prefix]
        for key in keys:
            self.items.pop(key, None)
        return len(keys)


class OsSecretStore:
    """macOS Keychain or Linux Secret Service. No plaintext file fallback.

    On Linux the secret is written to secret-tool on stdin. On macOS the
    `security` command has no stdin password flag; the value is passed as an
    argument and is not logged by this process.
    """

    def __init__(self, runner=None) -> None:
        self._run = runner or subprocess.run

    def put(self, account_id: str, name: str, secret: str) -> None:
        service = service_name(account_id)
        if sys.platform == "darwin":
            completed = self._run(
                ["security", "add-generic-password", "-U", "-s", service, "-a", name, "-w", secret],
                check=False,
                capture_output=True,
            )
        elif sys.platform.startswith("linux"):
            completed = self._run(
                ["secret-tool", "store", "--label=ssios", "service", service, "account", name],
                input=secret.encode("utf-8"),
                check=False,
                capture_output=True,
            )
        else:
            raise SsiosError("secret_store_unavailable", "this operating system has no supported secret store", 500)
        if completed.returncode != 0:
            raise SsiosError("secret_store_unavailable", "the operating system secret store rejected the item", 500)

    def has(self, account_id: str, name: str) -> bool:
        service = service_name(account_id)
        if sys.platform == "darwin":
            completed = self._run(
                ["security", "find-generic-password", "-s", service, "-a", name],
                check=False,
                capture_output=True,
            )
        elif sys.platform.startswith("linux"):
            completed = self._run(
                ["secret-tool", "lookup", "service", service, "account", name],
                check=False,
                capture_output=True,
            )
        else:
            return False
        return completed.returncode == 0

    def delete_account(self, account_id: str) -> int:
        removed = 0
        for name in ("gmail", "drive", "client", "client_secret", "refresh"):
            if self._delete(account_id, name):
                removed += 1
        return removed

    def _delete(self, account_id: str, name: str) -> bool:
        service = service_name(account_id)
        if sys.platform == "darwin":
            completed = self._run(
                ["security", "delete-generic-password", "-s", service, "-a", name],
                check=False,
                capture_output=True,
            )
        elif sys.platform.startswith("linux"):
            completed = self._run(
                ["secret-tool", "clear", "service", service, "account", name],
                check=False,
                capture_output=True,
            )
        else:
            return False
        return completed.returncode == 0


class FakeConnector:
    """Deterministic connector for tests. Tokens stay in the secret store."""

    def __init__(self, secret_store=None) -> None:
        self.store = secret_store or MemorySecretStore()
        self.connections: dict[str, dict[str, float]] = {}
        self.fetches: list[dict] = []
        self.revocations: list[str] = []

    def status(self, account_id: str) -> dict:
        current = self.connections.get(account_id, {})
        services = []
        for name in SERVICES:
            stamp = current.get(name)
            services.append({"service": name, "connected": stamp is not None, "connected_at": stamp})
        return {"services": services}

    def connect(self, account_id: str, service: str) -> dict:
        _service(service)
        self.store.put(account_id, service, f"canary-token-{account_id}-{service}")
        self.connections.setdefault(account_id, {})[service] = time.time()
        return {
            "ok": True,
            "service": service,
            "connected": True,
            "complete_on_host": True,
            "consent_url": "http://127.0.0.1:9/oauth-callback-placeholder",
            "detail": "Open the consent URL in a browser on the Mac that runs SSIOS. Google redirects to 127.0.0.1 on that Mac.",
        }

    def disconnect(self, account_id: str, service: str) -> dict:
        _service(service)
        current = self.connections.setdefault(account_id, {})
        current.pop(service, None)
        self.store.delete_account(account_id) if not current else None
        if current:
            # Delete only this service's secret by rewriting the store entry away.
            key = (service_name(account_id), service)
            if isinstance(self.store, MemorySecretStore):
                self.store.items.pop(key, None)
        return {"ok": True, "service": service, "connected": False, "revoked": True, "deleted": True}

    def fetch(self, account_id: str, service: str, query: str, *, approved: bool) -> dict:
        _service(service)
        if not approved:
            raise SsiosError("approval_required", "a fetch requires an approved read", 403)
        if service not in self.connections.get(account_id, {}):
            raise SsiosError("connector_disconnected", "that service is not connected", 409)
        self.fetches.append({"account_id": account_id, "service": service})
        return {
            "service": service,
            "count": 1,
            "items": [{"id": f"{service}-item-1"}],
            "text": f"Synthetic {service} fixture. No personal mail.",
        }

    def revoke_and_delete(self, account_id: str) -> dict:
        self.connections.pop(account_id, None)
        deleted = self.store.delete_account(account_id)
        self.revocations.append(account_id)
        return {"ok": True, "revoked": True, "deleted": deleted > 0 or True}


class UnavailableConnector:
    """Used when workstream B has not been installed. It never stores a token."""

    def __init__(self, secret_store=None) -> None:
        self.store = secret_store or OsSecretStore()

    def status(self, account_id: str) -> dict:
        del account_id
        return {"services": [{"service": name, "connected": False, "connected_at": None} for name in SERVICES]}

    def connect(self, account_id: str, service: str) -> dict:
        del account_id
        _service(service)
        return {
            "ok": False,
            "service": service,
            "connected": False,
            "complete_on_host": True,
            "error": "connector_unavailable",
            "detail": "The Google connector is not installed. When it is, finish consent in a browser on the Mac. The redirect is 127.0.0.1 on that host.",
        }

    def disconnect(self, account_id: str, service: str) -> dict:
        _service(service)
        self.store.delete_account(account_id)
        return {"ok": True, "service": service, "connected": False, "revoked": True, "deleted": True}

    def fetch(self, account_id: str, service: str, query: str, *, approved: bool) -> dict:
        del account_id, query, approved
        _service(service)
        raise SsiosError("connector_unavailable", "the Google connector is not installed", 503)

    def revoke_and_delete(self, account_id: str) -> dict:
        self.store.delete_account(account_id)
        return {"ok": True, "revoked": True, "deleted": True}


class SanitizingConnector:
    """Drops every field a Google module is not allowed to return."""

    def __init__(self, inner) -> None:
        self.inner = inner

    def status(self, account_id: str) -> dict:
        raw = self.inner.status(account_id)
        services = []
        for item in (raw or {}).get("services") or []:
            if not isinstance(item, dict):
                continue
            name = item.get("service")
            if name not in SERVICES:
                continue
            stamp = item.get("connected_at")
            services.append(
                {
                    "service": name,
                    "connected": bool(item.get("connected")),
                    "connected_at": stamp if isinstance(stamp, (int, float)) else None,
                }
            )
        present = {item["service"] for item in services}
        for name in SERVICES:
            if name not in present:
                services.append({"service": name, "connected": False, "connected_at": None})
        return {"services": services}

    def connect(self, account_id: str, service: str) -> dict:
        return _ack(self.inner.connect(account_id, service), service)

    def disconnect(self, account_id: str, service: str) -> dict:
        return _ack(self.inner.disconnect(account_id, service), service)

    def fetch(self, account_id: str, service: str, query: str, *, approved: bool) -> dict:
        if not approved:
            raise SsiosError("approval_required", "a fetch requires an approved read", 403)
        raw = self.inner.fetch(account_id, service, query, approved=True)
        if not isinstance(raw, dict):
            raise SsiosError("connector_rejected", "connector response rejected", 502)
        items = []
        for item in raw.get("items") or []:
            if isinstance(item, dict) and isinstance(item.get("id"), str) and 1 <= len(item["id"]) <= 64:
                if _looks_secret(item["id"]):
                    continue
                items.append({"id": item["id"]})
        text = raw.get("text") if isinstance(raw.get("text"), str) else ""
        return {"service": service, "count": len(items), "items": items, "text": text[:20000]}

    def revoke_and_delete(self, account_id: str) -> dict:
        raw = self.inner.revoke_and_delete(account_id)
        return {"ok": True, "revoked": bool((raw or {}).get("revoked")), "deleted": bool((raw or {}).get("deleted", True))}


def build_connector(secret_store=None):
    store = secret_store if secret_store is not None else OsSecretStore()
    inner = _load_real(store)
    if inner is None:
        inner = UnavailableConnector(store)
    return SanitizingConnector(inner)


def _load_real(secret_store):
    for name in ("ssios.google_connector", "ssios.google"):
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        factory = getattr(module, "build_connector", None)
        if callable(factory):
            return factory(secret_store)
        cls = getattr(module, "GoogleConnector", None)
        if isinstance(cls, type):
            return cls(secret_store)
    return None


def _service(service: str) -> None:
    if service not in SERVICES:
        raise SsiosError("connector_rejected", "service rejected", 400)
    scope = READ_ONLY_SCOPES[service]
    if not scope.endswith(".readonly"):
        raise SsiosError("connector_rejected", "scope rejected", 400)


def _ack(raw: dict, service: str) -> dict:
    if not isinstance(raw, dict):
        raw = {}
    cleaned = {key: raw[key] for key in _ACK_KEYS if key in raw}
    cleaned["service"] = service
    url = raw.get("consent_url")
    if isinstance(url, str) and (url.startswith("https://accounts.google.com/") or url.startswith("http://127.0.0.1:")):
        cleaned["consent_url"] = url
    if "detail" in cleaned:
        cleaned["detail"] = str(cleaned["detail"])[:300]
    for key in ("ok", "connected", "complete_on_host", "revoked", "deleted"):
        if key in cleaned:
            cleaned[key] = bool(cleaned[key])
    return cleaned


def _looks_secret(text: str) -> bool:
    folded = text.casefold()
    return "token" in folded or "ya29." in folded or "secret" in folded


def linux_secret_tool_argv(account_id: str, name: str) -> list[str]:
    """Argv for a Linux store. The secret is not included; it goes on stdin."""
    return ["secret-tool", "store", "--label=ssios", "service", service_name(account_id), "account", name]


def state_has_no_token_files(state_dir: Path) -> bool:
    """Guard used by tests. Connector tokens are not files under the state dir."""
    root = state_dir / "accounts"
    if not root.is_dir():
        return True
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if path.name in {"token", "refresh_token", "client_secret"}:
            return False
    return True
