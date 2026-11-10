"""Chat, connections, sign-up, and the admin console.

These pages require a household session. The admin console lists accounts and
connection flags. It does not read another account's mail, memory, or tokens.
"""

from __future__ import annotations

import html
import json

from ssios.chat import iter_turn
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
    if (name === "outcome") {
      var outcome = document.createElement("p");
      outcome.className = "outcome";
      outcome.textContent = payload.text || "";
      block.appendChild(outcome);
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
    card.className = (payload.destructive || String(payload.preview || "").indexOf("DESTRUCTIVE") === 0) ? "approval danger" : "approval";
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
    from ssios.ui import Page, _csrf, _page
    from ssios.ui_admin import (
        _admin,
        _admin_page,
        _admin_user,
        _audit_export,
        _audit_page,
        _change_password,
        _invite,
        _password_form,
    )

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
        return Page(200, _connections_page(plane, session, "", peer_ip))
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
    from ssios.ui import Page, _login_page
    from ssios.ui_admin import _join, _join_page, _signup, _signup_page

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
        from ssios.chat import english_connector_error

        return Page(exc.status, _page(session, "Chat", f"<p>{html.escape(english_connector_error(exc))}</p>"))
    return Page(200, _page(session, "Chat", _events_html(session, events)))


def _sse(plane, account, text, override, peer_ip, conversation):
    try:
        for event in iter_turn(plane, account, text, override, peer_ip, conversation):
            yield _frame(event)
    except SsiosError as exc:
        from ssios.chat import english_connector_error

        yield _frame({"event": "error", "data": {"detail": english_connector_error(exc)}})


def _frame(event: dict) -> str:
    return f"event: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"


def _events_html(session, events: list[dict]) -> str:
    parts = []
    conversation_id = ""
    for event in events:
        name = event["event"]
        data = event["data"]
        if name == "conversation":
            conversation_id = str(data.get("id") or "")
            parts.append("<p class=\"conversation\">" + html.escape(conversation_id) + "</p>")
        elif name == "route":
            parts.append("<p class=\"route\">Route: " + html.escape(str(data.get("route"))) + " (" + html.escape(str(data.get("source"))) + "). " + html.escape(str(data.get("reason"))) + "</p>")
        elif name == "token":
            parts.append(html.escape(str(data.get("text") or "")))
        elif name == "status":
            parts.append("<p>Guest: " + html.escape(str(data.get("phase") or "")) + " / " + html.escape(str(data.get("backend") or "")) + "</p>")
        elif name == "isolation":
            parts.append("<p>Isolation: " + html.escape(json.dumps(data, ensure_ascii=False)) + "</p>")
        elif name == "outcome":
            parts.append("<p class=\"outcome\">" + html.escape(str(data.get("text") or "")) + "</p>")
        elif name == "approval":
            parts.append(_approval_card(session, data))
        elif name == "error":
            parts.append("<p>" + html.escape(str(data.get("detail") or "")) + "</p>")
    back = "/ui/chat?conversation=" + html.escape(conversation_id) if conversation_id else "/ui/chat"
    return "<article class=\"turn\">" + "".join(parts) + "</article><p><a href=\"" + back + "\">Back to chat</a></p>"


def _approval_card(session, data: dict) -> str:
    from ssios.ui import _decision_form

    record = {
        "approval_id": data.get("id"),
        "action": data.get("action"),
        "payload_sha256": data.get("payload_sha256"),
        "expected_revision": data.get("expected_revision"),
        "bound_hash": data.get("bound_hash"),
    }
    kind = "approval danger" if data.get("destructive") or str(data.get("preview") or "").startswith("DESTRUCTIVE") else "approval"
    return "<article class=\"" + kind + "\"><pre>" + html.escape(str(data.get("preview") or "")) + "</pre>" + _decision_form(session, record) + "</article>"


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


def _connection_view(plane, account: dict) -> dict:
    from ssios.google_connector import session_for_account

    return plane.google.status(session_for_account(account))


def _connections_page(plane, session, notice: str, peer_ip: str = "127.0.0.1") -> str:
    from ssios.netguard import is_loopback
    from ssios.ui import _page

    account = session["account"]
    status = _connection_view(plane, account)
    rows = []
    connectors = status.get("connectors") if isinstance(status.get("connectors"), dict) else {}
    csrf = html.escape(str(session.get("csrf") or ""))
    forms = []
    for name in ("gmail", "drive", "calendar"):
        item = connectors.get(name) if isinstance(connectors.get(name), dict) else {}
        connected = bool(item.get("connected"))
        when = str(item.get("connected_at") or "")
        label = f"connected {when}".strip() if connected else "not connected"
        rows.append("<li>" + html.escape(name) + ": " + html.escape(label) + "</li>")
        action = "disconnect" if connected else "connect"
        forms.append(
            f"<form method=\"post\" action=\"/ui/connections\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
            f"<input type=\"hidden\" name=\"service\" value=\"{name}\">"
            f"<button name=\"op\" value=\"{action}\" type=\"submit\">{action.title()} {name}</button></form>"
        )
    if is_loopback(peer_ip):
        note = (
            "<p>Open the consent URL in a browser on this Mac. "
            "Google redirects to 127.0.0.1 on this Mac.</p>"
        )
    else:
        note = (
            "<p>This browser is not on the Mac that runs SSIOS. "
            "Copy the consent URL and open it in a browser on that Mac. "
            "Google redirects to 127.0.0.1 there. "
            "If this browser was redirected to a 127.0.0.1 address, paste that full address below. "
            "The consent check stays on the Mac.</p>"
            f"<form method=\"post\" action=\"/ui/connections\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
            "<label>Service <select name=\"service\"><option>gmail</option><option>drive</option><option>calendar</option></select></label>"
            "<label>Redirected 127.0.0.1 address <input name=\"redirect_url\"></label>"
            "<button name=\"op\" value=\"finish\" type=\"submit\">Finish consent</button></form>"
        )
    return _page(session, "Connections", notice + "<ul>" + "".join(rows) + "</ul>" + note + "".join(forms))


def _connection_action(plane, own, service: str, op: str, redirect_url: str) -> dict:
    if op == "connect":
        return plane.google.connect(own, service, open_browser=False)
    if op == "disconnect":
        return plane.google.disconnect(own, service)
    if op == "finish":
        return plane.google.submit_redirect(own, service, redirect_url)
    raise SsiosError("connector_rejected", "connection action rejected", 400)


def _connections_post(plane, session, body, peer_ip):
    from ssios.ui import Page
    from ssios.google_connector import session_for_account

    account = session["account"]
    account_id = account["id"]
    if body.get("account_id") and str(body.get("account_id")) != account_id:
        raise SsiosError("forbidden", "a session cannot act for another account", 403)
    service = str(body.get("service") or "")
    op = str(body.get("op") or "")
    own = session_for_account(account)
    try:
        result = _connection_action(plane, own, service, op, str(body.get("redirect_url") or ""))
    except SsiosError as exc:
        from ssios.chat import english_connector_error

        return Page(exc.status, _connections_page(plane, session, f"<p>{html.escape(english_connector_error(exc))}</p>", peer_ip))
    if op == "connect":
        plane.security_audit.write(actor=account_id, ip=peer_ip, action="connector.connect", target=service, result="ok")
    elif op == "disconnect":
        plane.security_audit.write(actor=account_id, ip=peer_ip, action="connector.disconnect", target=service, result="ok")
    elif op == "finish":
        plane.security_audit.write(actor=account_id, ip=peer_ip, action="connector.connect", target=service, result="ok")
    if op == "connect":
        url = html.escape(str(result.get("authorization_url") or ""))
        detail = (
            "<p>Consent URL is below. Open it on the Mac that runs SSIOS.</p>"
            f"<label>Consent URL <input readonly value=\"{url}\"></label>"
        )
    elif op == "finish":
        detail = "<p>Consent finished for this account.</p>"
    else:
        detail = "<p>Disconnected.</p>"
    return Page(200, _connections_page(plane, session, detail, peer_ip))


