"""Admin console, sign-up, and the household audit view."""

from __future__ import annotations

import html
import json
import time

from ssios.errors import SsiosError
from ssios.scope import AccountScope

def _signup(plane, body, peer_ip):
    from ssios.ui import Page

    try:
        plane.signup_limiter.check(peer_ip)
    except SsiosError as exc:
        plane.security_audit.write(actor="anonymous", ip=peer_ip, action="signup", target="rate", result="denied")
        return Page(exc.status, _signup_page(exc.detail))
    username = str(body.get("username") or "")
    password = str(body.get("password") or "")
    if body.get("role"):
        plane.signup_limiter.fail(peer_ip)
        plane.security_audit.write(actor="anonymous", ip=peer_ip, action="signup", target=username[:32], result="denied")
        return Page(403, _signup_page("Sign-up cannot choose a role."))
    try:
        account = plane.accounts.signup(username, password)
    except SsiosError as exc:
        plane.signup_limiter.fail(peer_ip)
        plane.security_audit.write(actor="anonymous", ip=peer_ip, action="signup", target="rejected", result="denied")
        return Page(exc.status, _signup_page(exc.detail))
    plane.signup_limiter.fail(peer_ip)
    plane.security_audit.write(actor=account["id"], ip=peer_ip, action="signup", target=account["id"], result="pending")
    return Page(200, _signup_page("Request received. An admin has to approve it before you can sign in."))


def _join(plane, body, peer_ip):
    from ssios.ui import Page

    try:
        account = plane.accounts.accept_invite(str(body.get("code") or ""), str(body.get("username") or ""), str(body.get("password") or ""))
    except SsiosError as exc:
        plane.security_audit.write(actor="anonymous", ip=peer_ip, action="signup", target="invite", result="denied")
        return Page(exc.status, _join_page("", exc.detail))
    plane.security_audit.write(actor=account["id"], ip=peer_ip, action="signup", target=account["id"], result="pending")
    return Page(200, _join_page("", "Invite accepted. An admin still has to approve the account."))


def _change_password(plane, session, body):
    from ssios.ui import Page, _page

    account = session["account"]
    first = str(body.get("password") or "")
    second = str(body.get("password_again") or "")
    if first != second:
        return Page(400, _page(session, "Change password", _password_form(session, "The two passwords did not match.")))
    try:
        plane.accounts.clear_must_change(account["id"], first)
    except SsiosError as exc:
        return Page(exc.status, _page(session, "Change password", _password_form(session, exc.detail)))
    plane.accounts.drop_session(session["id"])
    from ssios.ui import _login_page

    return Page(200, _login_page(""), cookie="ssios_session=; HttpOnly; SameSite=Strict; Path=/ui; Max-Age=0")


def _admin_page(plane, session, notice: str) -> str:
    from ssios.ui import _page

    rows = []
    csrf = html.escape(str(session.get("csrf") or ""))
    for account in plane.accounts.public_list():
        status = plane.google.admin_connection_status(account["id"])
        connectors = status.get("connectors") if isinstance(status.get("connectors"), dict) else {}
        flags = []
        for name in ("gmail", "drive", "calendar"):
            item = connectors.get(name) if isinstance(connectors.get(name), dict) else {}
            flags.append(f"{name} {'yes' if item.get('connected') else 'no'} {item.get('connected_at') or ''}".strip())
        rows.append(
            "<tr><td>" + html.escape(str(account["username"])) + "</td><td>" + html.escape(str(account["role"]))
            + "</td><td>" + html.escape(str(account["status"]))             + "</td><td>" + html.escape(", ".join(flags)) + "</td><td>"
            + "<details class=\"kebab\"><summary>Actions</summary>" + _admin_forms(csrf, account) + "</details></td></tr>"
        )
    chain = plane.security_audit.verify()
    body = (
        notice
        + "<p>Chain: " + html.escape(str(chain.get("detail"))) + "</p>"
        + "<table><tr><th>Username</th><th>Role</th><th>Status</th><th>Connections</th><th>Actions</th></tr>"
        + "".join(rows) + "</table>"
        + f"<form method=\"post\" action=\"/ui/admin/invites\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        + "<button type=\"submit\">Create invite</button></form>"
        + "<p><a href=\"/ui/admin/audit\">Audit</a></p>"
        + "<p>Deleting an account revokes its Google tokens, deletes its keychain entries, and archives local files. "
        + "The archive is not shown here. Resetting a password also revokes sessions and Google connections.</p>"
    )
    return _page(session, "Admin", body)


def _admin_forms(csrf: str, account: dict) -> str:
    status = str(account.get("status") or "")
    role = str(account.get("role") or "")
    forms = []
    if status == "pending":
        forms.extend((_admin_button(csrf, account, "approve", "Approve"), _admin_button(csrf, account, "reject", "Reject")))
    elif status == "active":
        forms.append(_admin_button(csrf, account, "disable", "Disable"))
        forms.append(_admin_reset(csrf, account))
        if role == "admin":
            forms.append(_admin_button(csrf, account, "role-member", "Make member"))
        else:
            forms.append(_admin_button(csrf, account, "role-admin", "Make admin"))
        forms.append(_admin_delete(csrf, account))
    elif status == "disabled":
        forms.extend((_admin_button(csrf, account, "enable", "Enable"), _admin_delete(csrf, account)))
    else:
        forms.append(_admin_delete(csrf, account))
    return "".join(forms)


def _admin_button(csrf: str, account: dict, op: str, label: str) -> str:
    account_id = html.escape(str(account["id"]))
    return (
        f"<form class=\"row\" method=\"post\" action=\"/ui/admin/users/{account_id}\">"
        f"<input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        f"<button name=\"op\" value=\"{op}\" type=\"submit\">{html.escape(label)}</button></form>"
    )


def _admin_reset(csrf: str, account: dict) -> str:
    account_id = html.escape(str(account["id"]))
    return (
        f"<form class=\"row\" method=\"post\" action=\"/ui/admin/users/{account_id}\">"
        f"<input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        "<label>New password <input name=\"password\" type=\"password\" autocomplete=\"new-password\"></label>"
        "<button name=\"op\" value=\"reset-password\" type=\"submit\">Reset password</button></form>"
    )


def _admin_delete(csrf: str, account: dict) -> str:
    account_id = html.escape(str(account["id"]))
    username = html.escape(str(account["username"]))
    return (
        f"<form class=\"row\" method=\"post\" action=\"/ui/admin/users/{account_id}\">"
        f"<input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        f"<label>Type delete {username} <input name=\"confirm\" placeholder=\"delete {username}\"></label>"
        "<button class=\"danger\" name=\"op\" value=\"delete\" type=\"submit\">Delete</button></form>"
    )


def _admin_user(plane, session, path, body, peer_ip):
    from ssios.ui import Page

    account_id = path.split("/")[-1]
    actor = session["account"]["id"]
    if account_id == actor and str(body.get("op") or "").startswith("role"):
        raise SsiosError("role_rejected", "an account cannot change its own role", 403)
    op = str(body.get("op") or "")
    target = plane.accounts.get(account_id)
    if target is None:
        raise SsiosError("not_found", "account not found", 404)
    _guard_admin_target(plane, actor, target, op)
    if op == "approve":
        plane.accounts.set_status(account_id, "active")
        AccountScope(plane, plane.accounts.get(account_id))
        action, result = "account.approve", "ok"
    elif op == "reject":
        plane.accounts.set_status(account_id, "rejected")
        action, result = "account.reject", "ok"
    elif op == "disable":
        plane.accounts.set_status(account_id, "disabled")
        action, result = "account.disable", "ok"
    elif op == "enable":
        plane.accounts.set_status(account_id, "active")
        action, result = "account.enable", "ok"
    elif op == "role-admin":
        updated = plane.accounts.set_role(actor, account_id, "admin")
        if updated.get("legacy_owner") and not target.get("legacy_owner"):
            raise SsiosError("role_rejected", "legacy ownership cannot be granted", 403)
        action, result = "account.role", "admin"
    elif op == "role-member":
        plane.accounts.set_role(actor, account_id, "member")
        action, result = "account.role", "member"
    elif op == "reset-password":
        plane.accounts.set_password(account_id, str(body.get("password") or ""), must_change=True)
        plane.google.revoke_and_delete(account_id)
        action, result = "account.reset", "revoked"
    elif op == "delete":
        expected = "delete " + str(target.get("username") or "")
        if str(body.get("confirm") or "") != expected:
            return Page(400, _admin_page(plane, session, "<p>Type the confirmation exactly. Nothing was deleted.</p>"))
        plane.google.revoke_and_delete(account_id)
        _archive_account(plane, account_id)
        plane.accounts.mark_archived(account_id)
        action, result = "account.delete", "archived"
    else:
        raise SsiosError("account_rejected", "admin action rejected", 400)
    plane.security_audit.write(actor=actor, ip=peer_ip, action=action, target=account_id, result=result, extra={"role": result} if op.startswith("role") else None)
    notice = "<p>Updated. Password resets revoke sessions and Google connections. Deletes archive local data after revocation.</p>"
    if op == "reset-password":
        notice = "<p>Password reset. Sessions and Google connections were revoked. The member must sign in and connect again.</p>"
    return Page(200, _admin_page(plane, session, notice))


def _archive_account(plane, account_id: str) -> None:
    import shutil

    home = plane.accounts.home(account_id)
    if not home.is_dir():
        return
    archive = plane.accounts.root / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    archive.chmod(0o700)
    destination = archive / f"{account_id}-{int(time.time())}"
    shutil.move(str(home), str(destination))
    destination.chmod(0o700)


def _invite(plane, session, peer_ip):
    from ssios.ui import Page, _page

    raw = plane.accounts.create_invite(session["account"]["id"])
    plane.security_audit.write(actor=session["account"]["id"], ip=peer_ip, action="account.invite", target="invite", result="ok")
    link = "/ui/join?code=" + html.escape(raw)
    return Page(200, _page(session, "Invite", f"<p>One-time invite. It stays pending until you approve the new account.</p><p><a href=\"{link}\">{link}</a></p>"))


def _audit_page(plane, session, query, *, admin: bool) -> str:
    from ssios.ui import _page

    actor = str(query.get("actor") or "")
    action = str(query.get("action") or "")
    if not admin:
        actor = session["account"]["id"]
    since = _float_or_none(query.get("since"))
    until = _float_or_none(query.get("until"))
    rows = plane.security_audit.filtered(actor=actor, action=action, since=since, until=until)
    if not admin:
        rows = [row for row in rows if row.get("actor") == session["account"]["id"]]
    lines = []
    for row in rows[-200:]:
        lines.append("<li>" + html.escape(json.dumps({key: row.get(key) for key in ("ts", "actor", "ip", "action", "target", "result")}, ensure_ascii=False)) + "</li>")
    chain = plane.security_audit.verify()
    export = ""
    filters = ""
    if admin:
        filters = (
            "<form method=\"get\" action=\"/ui/admin/audit\">"
            "<label>User <input name=\"actor\"></label>"
            "<label>Action <input name=\"action\"></label>"
            "<label>Since <input name=\"since\"></label>"
            "<label>Until <input name=\"until\"></label>"
            "<button type=\"submit\">Filter</button></form>"
            "<p><a href=\"/ui/admin/audit/export\">Export</a></p>"
        )
    body = filters + "<p>Chain: " + html.escape(str(chain.get("detail"))) + "</p><ul>" + "".join(lines) + "</ul>"
    return _page(session, "Audit" if admin else "Activity", body)


def _audit_export(plane, query):
    from ssios.ui import Page

    rows = plane.security_audit.filtered(
        actor=str(query.get("actor") or ""),
        action=str(query.get("action") or ""),
        since=_float_or_none(query.get("since")),
        until=_float_or_none(query.get("until")),
    )
    safe = [{key: row.get(key) for key in ("ts", "actor", "ip", "action", "target", "result", "extra")} for row in rows]
    return Page(200, json.dumps({"entries": safe}, ensure_ascii=False, indent=2), content_type="application/json; charset=utf-8")


def _password_form(session, notice: str) -> str:
    csrf = html.escape(str(session.get("csrf") or ""))
    return (
        f"<p>{html.escape(notice)}</p><form method=\"post\" action=\"/ui/password\">"
        f"<input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        "<label>New password <input name=\"password\" type=\"password\" autocomplete=\"new-password\"></label>"
        "<label>Again <input name=\"password_again\" type=\"password\" autocomplete=\"new-password\"></label>"
        "<button type=\"submit\">Change password</button></form>"
    )


def _signup_page(notice: str) -> str:
    from ssios.ui import _STYLE

    return (
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><title>Request an account</title>"
        + _STYLE
        + "<body><main><h1>Request an account</h1><p>"
        + html.escape(notice)
        + "</p><form method=\"post\" action=\"/ui/signup\">"
        "<label>Username <input name=\"username\"></label>"
        "<label>Password <input name=\"password\" type=\"password\"></label>"
        "<button type=\"submit\">Request account</button></form>"
        "<p><a href=\"/ui/login\">Sign in</a></p></main></body></html>"
    )


def _join_page(code: str, notice: str) -> str:
    from ssios.ui import _STYLE

    return (
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><title>Join</title>"
        + _STYLE
        + "<body><main><h1>Join with an invite</h1><p>"
        + html.escape(notice)
        + "</p><form method=\"post\" action=\"/ui/join\">"
        f"<label>Invite <input name=\"code\" value=\"{html.escape(code)}\"></label>"
        "<label>Username <input name=\"username\"></label>"
        "<label>Password <input name=\"password\" type=\"password\"></label>"
        "<button type=\"submit\">Create pending account</button></form></main></body></html>"
    )


def _guard_admin_target(plane, actor_id: str, target: dict, op: str) -> None:
    if target["id"] == actor_id and op in {"delete", "disable", "role-member", "role-admin"}:
        raise SsiosError("role_rejected", "an admin cannot delete, disable, or change their own role here", 403)
    if target.get("role") == "admin" and target.get("status") == "active" and op in {"delete", "disable", "role-member"}:
        active = [
            item
            for item in plane.accounts.public_list()
            if item.get("role") == "admin" and item.get("status") == "active"
        ]
        if len(active) <= 1:
            raise SsiosError("role_rejected", "the last active admin cannot be removed", 403)


def _admin(account: dict) -> None:
    if account.get("role") != "admin" or account.get("status") != "active":
        raise SsiosError("forbidden", "admin role required", 403)


def _float_or_none(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
