"""Chat, connections, sign-up, and the admin console.

These pages require a household session. The admin console lists accounts and
connection flags. It does not read another account's mail, memory, or tokens.
"""

from __future__ import annotations

import html
import json
import time

from ssios.chat import continue_after_fetch, iter_turn
from ssios.config import lan_enabled
from ssios.errors import SsiosError
from ssios.scope import AccountScope

CHAT_JS = r"""
(function () {
  var form = document.getElementById("chat-form");
  var log = document.getElementById("chat-log");
  if (!form || !log) return;
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var params = new URLSearchParams(new FormData(form));
    var block = document.createElement("article");
    block.className = "turn";
    log.appendChild(block);
    var user = document.createElement("p");
    user.textContent = params.get("message") || "";
    block.appendChild(user);
    var answer = document.createElement("p");
    block.appendChild(answer);
    fetch(form.action, {
      method: "POST",
      headers: {"Accept": "text/event-stream", "Content-Type": "application/x-www-form-urlencoded"},
      body: params.toString(),
      credentials: "same-origin"
    }).then(function (response) {
      if (!response.ok || !response.body) {
        answer.textContent = "The request was rejected.";
        return;
      }
      var reader = response.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";
      function pump() {
        return reader.read().then(function (chunk) {
          if (chunk.done) return;
          buffer += decoder.decode(chunk.value, {stream: true});
          var parts = buffer.split("\n\n");
          buffer = parts.pop();
          parts.forEach(function (part) { applyEvent(block, answer, part); });
          return pump();
        });
      }
      return pump();
    }).catch(function () {
      answer.textContent = "The stream stopped.";
    });
    form.querySelector("textarea").value = "";
  });

  function applyEvent(block, answer, raw) {
    var name = "message";
    var data = "";
    raw.split("\n").forEach(function (line) {
      if (line.indexOf("event:") === 0) name = line.slice(6).trim();
      if (line.indexOf("data:") === 0) data += line.slice(5).trim();
    });
    if (!data) return;
    var payload = {};
    try { payload = JSON.parse(data); } catch (err) { return; }
    if (name === "token") answer.textContent += payload.text || "";
    if (name === "route") {
      var note = document.createElement("p");
      note.className = "route";
      note.textContent = "Route: " + (payload.route || "") + " (" + (payload.source || "") + "). " + (payload.reason || "");
      block.appendChild(note);
    }
    if (name === "status") {
      var status = document.createElement("p");
      status.textContent = "Guest: " + (payload.phase || "") + (payload.backend ? " / " + payload.backend : "");
      block.appendChild(status);
    }
    if (name === "isolation") {
      var iso = document.createElement("p");
      iso.textContent = "Isolation: nic " + JSON.stringify(payload.nic || []) + ", outbound " + String(payload.outbound) + ", marker " + String(payload.marker) + ", sandbox denied " + String(payload.sandbox_denied) + ", outside write " + String(payload.outside_write);
      block.appendChild(iso);
    }
    if (name === "approval") addApproval(block, payload);
    if (name === "error") answer.textContent = payload.detail || "The turn failed.";
    if (name === "conversation" && payload.id) {
      var hidden = form.querySelector("input[name=conversation]");
      if (hidden) hidden.value = payload.id;
    }
  }

  function addApproval(block, payload) {
    var card = document.createElement("article");
    card.className = "approval";
    var pre = document.createElement("pre");
    pre.textContent = payload.preview || "";
    card.appendChild(pre);
    var approval = document.createElement("form");
    approval.method = "post";
    approval.action = "/ui/approvals/" + encodeURIComponent(payload.id || "");
    ["csrf", "action", "payload_sha256", "expected_revision", "bound_hash"].forEach(function (key) {
      var input = document.createElement("input");
      input.type = "hidden";
      input.name = key;
      input.value = key === "csrf" ? (form.querySelector("input[name=csrf]") || {}).value || "" : (payload[key] || "");
      approval.appendChild(input);
    });
    ["approve", "deny"].forEach(function (value) {
      var button = document.createElement("button");
      button.type = "submit";
      button.name = "decision";
      button.value = value;
      button.textContent = value === "approve" ? "Approve" : "Deny";
      approval.appendChild(button);
    });
    card.appendChild(approval);
    block.appendChild(card);
  }
})();
"""


def handle_household(plane, method: str, path: str, session: dict | None, body: dict, query: dict, peer_ip: str, headers):
    from ssios.ui import Page, _csrf, _decision_form, _page

    if session is None and path in {"/ui/signup", "/ui/join"}:
        return _public_account(plane, method, path, body, query, peer_ip)
    if session is None:
        return None
    account = session.get("account")
    if not isinstance(account, dict):
        return None
    if session.get("must_change_password") and path not in {"/ui/password", "/ui/logout"}:
        return Page(200, _page(session, "Change password", _password_form(session, "")))
    if path == "/ui/assets/chat.js" and method == "GET":
        return Page(200, CHAT_JS, content_type="text/javascript; charset=utf-8")
    if headers.get("Upgrade", "").lower() == "websocket":
        return Page(404, _page(session, "Not available", "<p>WebSocket is not enabled. Chat uses a same-origin event stream.</p>"))
    if path == "/ui/chat" and method == "GET":
        return Page(200, _chat_page(plane, session, query.get("conversation", "")))
    if path == "/ui/chat/send" and method == "POST":
        _csrf(session, body)
        return _send(plane, session, body, peer_ip, headers)
    if path == "/ui/chat/events" and method == "GET":
        return Page(200, "", stream=iter(["event: ready\ndata: {}\n\n"]), content_type="text/event-stream; charset=utf-8")
    if path == "/ui/connections" and method == "GET":
        return Page(200, _connections_page(plane, session, ""))
    if path == "/ui/connections" and method == "POST":
        _csrf(session, body)
        return _connections_post(plane, session, body, peer_ip)
    if path == "/ui/password" and method == "GET":
        return Page(200, _page(session, "Change password", _password_form(session, "")))
    if path == "/ui/password" and method == "POST":
        _csrf(session, body)
        return _change_password(plane, session, body)
    if path == "/ui/audit" and method == "GET":
        return Page(200, _audit_page(plane, session, query, admin=False))
    if path == "/ui/admin" and method == "GET":
        _admin(account)
        return Page(200, _admin_page(plane, session, ""))
    if path == "/ui/admin/audit" and method == "GET":
        _admin(account)
        return Page(200, _audit_page(plane, session, query, admin=True))
    if path == "/ui/admin/audit/export" and method == "GET":
        _admin(account)
        return _audit_export(plane, query)
    if path == "/ui/admin/invites" and method == "POST":
        _admin(account)
        _csrf(session, body)
        return _invite(plane, session, peer_ip)
    if path.startswith("/ui/admin/users/") and method == "POST":
        _admin(account)
        _csrf(session, body)
        return _admin_user(plane, session, path, body, peer_ip)
    return None


def _public_account(plane, method, path, body, query, peer_ip):
    from ssios.ui import Page, _login_page, _page

    if path == "/ui/signup" and method == "GET":
        return Page(200, _signup_page(""))
    if path == "/ui/signup" and method == "POST":
        return _signup(plane, body, peer_ip)
    if path == "/ui/join" and method == "GET":
        return Page(200, _join_page(query.get("code", ""), ""))
    if path == "/ui/join" and method == "POST":
        return _join(plane, body, peer_ip)
    return Page(404, _login_page(""))


def _send(plane, session, body, peer_ip, headers):
    from ssios.ui import Page, _page

    account = session["account"]
    text = str(body.get("message") or "")
    override = str(body.get("override") or "auto")
    conversation = str(body.get("conversation") or "")
    accept = str(headers.get("Accept") or "")
    if "text/event-stream" in accept:
        return Page(
            200,
            "",
            stream=_sse(plane, account, text, override, peer_ip, conversation),
            content_type="text/event-stream; charset=utf-8",
        )
    try:
        events = list(iter_turn(plane, account, text, override, peer_ip, conversation))
    except SsiosError as exc:
        return Page(exc.status, _page(session, "Chat", f"<p>{html.escape(exc.detail)}</p>"))
    return Page(200, _page(session, "Chat", _events_html(session, events)))


def _sse(plane, account, text, override, peer_ip, conversation):
    try:
        for event in iter_turn(plane, account, text, override, peer_ip, conversation):
            yield _frame(event)
    except SsiosError as exc:
        yield _frame({"event": "error", "data": {"detail": exc.detail}})


def _frame(event: dict) -> str:
    return f"event: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"


def _events_html(session, events: list[dict]) -> str:
    parts = []
    for event in events:
        name = event["event"]
        data = event["data"]
        if name == "route":
            parts.append("<p class=\"route\">Route: " + html.escape(str(data.get("route"))) + " (" + html.escape(str(data.get("source"))) + "). " + html.escape(str(data.get("reason"))) + "</p>")
        elif name == "token":
            parts.append(html.escape(str(data.get("text") or "")))
        elif name == "status":
            parts.append("<p>Guest: " + html.escape(str(data.get("phase") or "")) + " / " + html.escape(str(data.get("backend") or "")) + "</p>")
        elif name == "isolation":
            parts.append("<p>Isolation: " + html.escape(json.dumps(data, ensure_ascii=False)) + "</p>")
        elif name == "approval":
            parts.append(_approval_card(session, data))
        elif name == "error":
            parts.append("<p>" + html.escape(str(data.get("detail") or "")) + "</p>")
    return "<article class=\"turn\">" + "".join(parts) + "</article><p><a href=\"/ui/chat\">Back to chat</a></p>"


def _approval_card(session, data: dict) -> str:
    from ssios.ui import _decision_form

    record = {
        "approval_id": data.get("id"),
        "action": data.get("action"),
        "payload_sha256": data.get("payload_sha256"),
        "expected_revision": data.get("expected_revision"),
        "bound_hash": data.get("bound_hash"),
    }
    return "<article class=\"approval\"><pre>" + html.escape(str(data.get("preview") or "")) + "</pre>" + _decision_form(session, record) + "</article>"


def _chat_page(plane, session, conversation_id: str) -> str:
    from ssios.ui import _page

    scope = AccountScope(plane, session["account"])
    rows = []
    for item in scope.chat().list_conversations():
        rows.append(f"<li><a href=\"/ui/chat?conversation={html.escape(item['id'])}\">{html.escape(str(item['title']))}</a></li>")
    transcript = ""
    if conversation_id:
        try:
            record = scope.chat().get(conversation_id)
        except SsiosError:
            record = None
        if record:
            blocks = []
            for message in record.get("messages") or []:
                blocks.append("<p><strong>" + html.escape(str(message.get("role") or "")) + "</strong> " + html.escape(str(message.get("text") or "")) + "</p>")
            transcript = "".join(blocks)
    csrf = html.escape(str(session.get("csrf") or ""))
    conv = html.escape(conversation_id)
    body = (
        "<ul>" + "".join(rows) + "</ul>"
        + "<div id=\"chat-log\">" + transcript + "</div>"
        + f"<form id=\"chat-form\" method=\"post\" action=\"/ui/chat/send\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        + f"<input type=\"hidden\" name=\"conversation\" value=\"{conv}\">"
        + "<label>Message <textarea name=\"message\" rows=\"4\" required></textarea></label>"
        + "<label>Route <select name=\"override\"><option value=\"auto\">Auto</option>"
        + "<option value=\"force_direct\">Force direct</option><option value=\"force_vm\">Force virtual machine</option></select></label>"
        + "<button type=\"submit\">Send</button></form>"
        + "<script src=\"/ui/assets/chat.js\"></script>"
        + "<noscript><p>The form still sends the message when the script does not run.</p></noscript>"
    )
    return _page(session, "Chat", body)


def _connections_page(plane, session, notice: str) -> str:
    from ssios.ui import _page

    status = plane.connector.status(session["account"]["id"])
    rows = []
    for item in status.get("services") or []:
        stamp = item.get("connected_at")
        when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(stamp)) if isinstance(stamp, (int, float)) else "not connected"
        rows.append(
            "<li>" + html.escape(str(item.get("service"))) + ": " + ("connected" if item.get("connected") else "not connected") + " / " + html.escape(when) + "</li>"
        )
    csrf = html.escape(str(session.get("csrf") or ""))
    forms = []
    for service in ("gmail", "drive"):
        forms.append(
            f"<form method=\"post\" action=\"/ui/connections\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
            f"<input type=\"hidden\" name=\"service\" value=\"{service}\">"
            f"<button name=\"op\" value=\"connect\" type=\"submit\">Connect {service}</button>"
            f"<button name=\"op\" value=\"disconnect\" type=\"submit\">Disconnect {service}</button></form>"
        )
    note = (
        "<p>Consent finishes in a browser on the Mac that runs SSIOS. "
        "Google redirects to 127.0.0.1 on that Mac. A browser on another computer cannot complete that redirect. "
        "Copy the consent URL and open it on the host, or click Connect while you are at the Mac.</p>"
    )
    return _page(session, "Connections", notice + "<ul>" + "".join(rows) + "</ul>" + note + "".join(forms))


def _connections_post(plane, session, body, peer_ip):
    from ssios.ui import Page

    account_id = session["account"]["id"]
    if body.get("account_id") and str(body.get("account_id")) != account_id:
        raise SsiosError("forbidden", "a session cannot act for another account", 403)
    service = str(body.get("service") or "")
    op = str(body.get("op") or "")
    if op == "connect":
        result = plane.connector.connect(account_id, service)
        plane.security_audit.write(actor=account_id, ip=peer_ip, action="connector.connect", target=service, result="ok" if result.get("ok", True) else "unavailable")
    elif op == "disconnect":
        result = plane.connector.disconnect(account_id, service)
        plane.security_audit.write(actor=account_id, ip=peer_ip, action="connector.disconnect", target=service, result="ok")
    else:
        raise SsiosError("connector_rejected", "connection action rejected", 400)
    detail = html.escape(str(result.get("detail") or result.get("error") or "Updated."))
    url = result.get("consent_url")
    extra = f"<p>Consent URL: {html.escape(str(url))}</p>" if url else ""
    return Page(200, _connections_page(plane, session, f"<p>{detail}</p>{extra}"))


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
        status = plane.connector.status(account["id"])
        flags = []
        for item in status.get("services") or []:
            when = item.get("connected_at")
            stamp = time.strftime("%Y-%m-%d", time.gmtime(when)) if isinstance(when, (int, float)) else ""
            flags.append(f"{item.get('service')} {'yes' if item.get('connected') else 'no'} {stamp}".strip())
        rows.append(
            "<tr><td>" + html.escape(str(account["username"])) + "</td><td>" + html.escape(str(account["role"]))
            + "</td><td>" + html.escape(str(account["status"])) + "</td><td>" + html.escape(", ".join(flags)) + "</td><td>"
            + _admin_forms(csrf, account) + "</td></tr>"
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
    account_id = html.escape(str(account["id"]))
    username = html.escape(str(account["username"]))
    base = f"<form class=\"row\" method=\"post\" action=\"/ui/admin/users/{account_id}\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
    buttons = (
        "<button name=\"op\" value=\"approve\" type=\"submit\">Approve</button>"
        "<button name=\"op\" value=\"reject\" type=\"submit\">Reject</button>"
        "<button name=\"op\" value=\"enable\" type=\"submit\">Enable</button>"
        "<button name=\"op\" value=\"disable\" type=\"submit\">Disable</button>"
        "<button name=\"op\" value=\"role-admin\" type=\"submit\">Make admin</button>"
        "<button name=\"op\" value=\"role-member\" type=\"submit\">Make member</button>"
        "</form>"
    )
    reset = (
        f"{base}<input name=\"password\" type=\"password\" autocomplete=\"new-password\">"
        "<button name=\"op\" value=\"reset-password\" type=\"submit\">Reset password</button></form>"
    )
    delete = (
        f"{base}<input name=\"confirm\" placeholder=\"delete {username}\">"
        "<button name=\"op\" value=\"delete\" type=\"submit\">Delete</button></form>"
    )
    return base + buttons + reset + delete


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
        plane.connector.revoke_and_delete(account_id)
        action, result = "account.reset", "revoked"
    elif op == "delete":
        expected = "delete " + str(target.get("username") or "")
        if str(body.get("confirm") or "") != expected:
            return Page(400, _admin_page(plane, session, "<p>Type the confirmation exactly. Nothing was deleted.</p>"))
        plane.connector.revoke_and_delete(account_id)
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
