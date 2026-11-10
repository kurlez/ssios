"""Household accounts. The first admin references the existing state in place.

Passwords are scrypt hashes. Sessions store a csrf secret and an account id,
never a password. Web role changes cannot set legacy_owner and cannot raise
the caller's own role.
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from pathlib import Path

from ssios.errors import SsiosError
from ssios.passwords import hash_password, validate_password, verify_password
from ssios.util import read_json, secure_mkdir, sha256_text, write_json

_USERNAME = re.compile(r"^[a-z][a-z0-9-]{2,31}$")
_ACCOUNT = re.compile(r"^acct-[0-9a-f]{16}$")
SESSION_TTL = 12 * 3600
INVITE_TTL = 3600
PUBLIC_FIELDS = (
    "id",
    "username",
    "role",
    "status",
    "created_at",
    "display_label",
    "must_change_password",
    "legacy_owner",
)


def public_account(account: dict) -> dict:
    return {key: account.get(key) for key in PUBLIC_FIELDS}


class AccountStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.root = state_dir / "accounts"
        self.registry_path = self.root / "registry.json"
        self.sessions_path = self.root / "sessions.json"
        self.invites_path = self.root / "invites.json"
        self._lock = threading.RLock()
        self.users: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.invites: dict[str, dict] = {}

    def load(self) -> None:
        with self._lock:
            secure_mkdir(self.root)
            self._read()

    def _read(self) -> None:
        data = read_json(self.registry_path, {"users": {}})
        users = data.get("users") if isinstance(data, dict) else {}
        self.users = {}
        if isinstance(users, dict):
            for key, value in users.items():
                if isinstance(value, dict) and _ACCOUNT.fullmatch(str(key)) and value.get("id") == key:
                    self.users[key] = value
        sessions = read_json(self.sessions_path, {"sessions": {}})
        raw_sessions = sessions.get("sessions") if isinstance(sessions, dict) else {}
        self.sessions = raw_sessions if isinstance(raw_sessions, dict) else {}
        invites = read_json(self.invites_path, {"invites": {}})
        raw_invites = invites.get("invites") if isinstance(invites, dict) else {}
        self.invites = raw_invites if isinstance(raw_invites, dict) else {}

    def _save_users(self) -> None:
        write_json(self.registry_path, {"users": self.users})

    def _save_sessions(self) -> None:
        write_json(self.sessions_path, {"sessions": self.sessions})

    def _save_invites(self) -> None:
        write_json(self.invites_path, {"invites": self.invites})

    def add_admin(self, username: str, password: str) -> dict:
        """Host bootstrap. The first admin owns the existing memory tree in place."""
        with self._lock:
            self.load()
            legacy = not any(item.get("legacy_owner") for item in self.users.values())
            return self._add(username, password, role="admin", status="active", legacy_owner=legacy, must_change_password=False)

    def add_member(self, username: str, password: str, *, status: str = "active") -> dict:
        with self._lock:
            self.load()
            return self._add(username, password, role="member", status=status, legacy_owner=False, must_change_password=False)

    def signup(self, username: str, password: str) -> dict:
        with self._lock:
            self.load()
            account = self._add(username, password, role="member", status="pending", legacy_owner=False, must_change_password=False)
        return public_account(account)

    def _add(
        self,
        username: str,
        password: str,
        *,
        role: str,
        status: str,
        legacy_owner: bool,
        must_change_password: bool,
    ) -> dict:
        _username(username)
        validate_password(password, username)
        if role not in ("admin", "member"):
            raise SsiosError("role_rejected", "role rejected", 400)
        if status not in ("active", "pending"):
            raise SsiosError("account_rejected", "status rejected", 400)
        if any(item.get("username") == username for item in self.users.values()):
            raise SsiosError("account_exists", "username is taken", 409)
        if legacy_owner and any(item.get("legacy_owner") for item in self.users.values()):
            raise SsiosError("legacy_owner_exists", "the original store already has an owner", 409)
        account_id = "acct-" + secrets.token_hex(8)
        account = {
            "id": account_id,
            "username": username,
            "password_hash": hash_password(password),
            "role": role,
            "status": status,
            "created_at": time.time(),
            "display_label": "",
            "must_change_password": must_change_password,
            "legacy_owner": bool(legacy_owner),
        }
        self.users[account_id] = account
        self._save_users()
        return account

    def authenticate(self, username: str, password: str) -> dict:
        with self._lock:
            self.load()
            found = None
            for item in self.users.values():
                if item.get("username") == username:
                    found = item
                    break
            if found is None or not verify_password(password, str(found.get("password_hash") or "")):
                raise SsiosError("unauthorized", "authentication required", 401)
            if found.get("status") != "active":
                raise SsiosError("account_inactive", "account is not active", 403)
            return public_account(found)

    def get(self, account_id: str) -> dict | None:
        with self._lock:
            self.load()
            item = self.users.get(account_id)
            if not isinstance(item, dict):
                return None
            return public_account(item)

    def public_list(self) -> list[dict]:
        with self._lock:
            self.load()
            return [public_account(item) for item in self.users.values()]

    def set_status(self, account_id: str, status: str) -> dict:
        if status not in ("active", "disabled", "rejected", "pending"):
            raise SsiosError("account_rejected", "status rejected", 400)
        with self._lock:
            self.load()
            item = self._require(account_id)
            item["status"] = status
            self._save_users()
            if status != "active":
                self._drop_account_sessions(account_id)
            return public_account(item)

    def set_role(self, actor_id: str, account_id: str, role: str) -> dict:
        if role not in ("admin", "member"):
            raise SsiosError("role_rejected", "role rejected", 400)
        if actor_id == account_id:
            raise SsiosError("role_rejected", "an account cannot change its own role", 403)
        with self._lock:
            self.load()
            item = self._require(account_id)
            item["role"] = role
            item["legacy_owner"] = bool(item.get("legacy_owner"))
            self._save_users()
            return public_account(item)

    def set_password(self, account_id: str, password: str, *, must_change: bool) -> dict:
        with self._lock:
            self.load()
            item = self._require(account_id)
            validate_password(password, str(item.get("username") or ""))
            item["password_hash"] = hash_password(password)
            item["must_change_password"] = bool(must_change)
            self._save_users()
            self._drop_account_sessions(account_id)
            return public_account(item)

    def clear_must_change(self, account_id: str, password: str) -> dict:
        return self.set_password(account_id, password, must_change=False)

    def set_display_label(self, account_id: str, label: str) -> dict:
        text = " ".join(str(label or "").split())
        if len(text) > 40:
            raise SsiosError("account_rejected", "display label is too long", 400)
        with self._lock:
            self.load()
            item = self._require(account_id)
            item["display_label"] = text
            self._save_users()
            return public_account(item)

    def mark_archived(self, account_id: str) -> dict:
        with self._lock:
            self.load()
            item = self._require(account_id)
            item["status"] = "archived"
            self._save_users()
            self._drop_account_sessions(account_id)
            return public_account(item)

    def create_invite(self, actor_id: str) -> str:
        raw = secrets.token_urlsafe(18)
        with self._lock:
            self.load()
            self.invites[sha256_text(raw)] = {
                "id": "inv-" + secrets.token_hex(4),
                "actor": actor_id,
                "expires": time.time() + INVITE_TTL,
                "used": False,
            }
            self._save_invites()
        return raw

    def accept_invite(self, raw: str, username: str, password: str) -> dict:
        digest = sha256_text(raw)
        with self._lock:
            self.load()
            invite = self.invites.get(digest)
            if not isinstance(invite, dict) or invite.get("used") or float(invite.get("expires") or 0) < time.time():
                raise SsiosError("invite_rejected", "invite is not valid", 400)
            account = self._add(username, password, role="member", status="pending", legacy_owner=False, must_change_password=False)
            invite["used"] = True
            self._save_invites()
        return public_account(account)

    def create_session(self, account_id: str) -> tuple[str, str]:
        session_id = secrets.token_urlsafe(24)
        csrf = secrets.token_urlsafe(18)
        with self._lock:
            self.load()
            if account_id not in self.users:
                raise SsiosError("not_found", "account not found", 404)
            self.sessions[session_id] = {
                "account_id": account_id,
                "csrf": csrf,
                "expires": time.time() + SESSION_TTL,
            }
            self._save_sessions()
        return session_id, csrf

    def read_session(self, session_id: str) -> dict | None:
        with self._lock:
            self.load()
            record = self.sessions.get(session_id)
            if not isinstance(record, dict):
                return None
            if float(record.get("expires") or 0) < time.time():
                return None
            account = self.users.get(str(record.get("account_id") or ""))
            if not isinstance(account, dict) or account.get("status") != "active":
                return None
            return {
                "id": session_id,
                "csrf": str(record.get("csrf") or ""),
                "account": public_account(account),
                "must_change_password": bool(account.get("must_change_password")),
            }

    def drop_session(self, session_id: str) -> None:
        with self._lock:
            self.load()
            if session_id in self.sessions:
                self.sessions.pop(session_id, None)
                self._save_sessions()

    def drop_account_sessions(self, account_id: str) -> None:
        with self._lock:
            self.load()
            self._drop_account_sessions(account_id)

    def _drop_account_sessions(self, account_id: str) -> None:
        self.sessions = {key: value for key, value in self.sessions.items() if value.get("account_id") != account_id}
        self._save_sessions()

    def _require(self, account_id: str) -> dict:
        item = self.users.get(account_id)
        if not isinstance(item, dict):
            raise SsiosError("not_found", "account not found", 404)
        return item

    def home(self, account_id: str) -> Path:
        if not _ACCOUNT.fullmatch(account_id):
            raise SsiosError("path_rejected", "account id rejected", 400)
        path = (self.root / account_id).resolve()
        root = self.root.resolve()
        if root not in path.parents:
            raise SsiosError("path_rejected", "account path rejected", 400)
        return path

    def legacy_admin(self) -> dict | None:
        with self._lock:
            self.load()
            for item in self.users.values():
                if item.get("legacy_owner") and item.get("role") == "admin" and item.get("status") != "archived":
                    return public_account(item)
        return None


def _username(username: str) -> None:
    if not isinstance(username, str) or not _USERNAME.fullmatch(username):
        raise SsiosError("account_rejected", "username must be 3 to 32 lowercase letters, digits, or hyphens", 400)
