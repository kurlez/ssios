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
  var column = log ? log.querySelector(".column") : null;
  var dataNode = document.getElementById("chat-data");
  if (!form || !column) return;
  var csrf = (form.querySelector("input[name=csrf]") || {}).value || "";
  var turnFailed = false;
  var state = {};
  try { state = JSON.parse(dataNode ? dataNode.textContent || "{}" : "{}"); } catch (err) { state = {}; }
  (state.messages || []).forEach(renderMessage);
  var box = form.querySelector("textarea");
  box.addEventListener("input", grow);
  box.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (box.value.trim()) form.requestSubmit();
    }
  });
  var toggle = document.createElement("button");
  toggle.type = "button";
  toggle.id = "sidebar-toggle";
  toggle.textContent = "Conversations";
  toggle.setAttribute("aria-controls", "sidebar");
  toggle.setAttribute("aria-expanded", "true");
  toggle.addEventListener("click", function () {
    var open = document.body.classList.toggle("sidebar-open");
    document.body.classList.toggle("sidebar-closed", !open && window.innerWidth > 800);
    if (window.innerWidth > 800) document.body.classList.toggle("sidebar-closed");
    toggle.setAttribute("aria-expanded", String(!document.body.classList.contains("sidebar-closed") && (window.innerWidth > 800 || document.body.classList.contains("sidebar-open"))));
  });
  var top = document.querySelector(".top");
  if (top) top.insertBefore(toggle, top.firstChild);
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var text = box.value;
    renderMessage({role: "user", text: text});
    var answer = renderMessage({role: "assistant", text: ""});
    var typing = document.createElement("p");
    typing.className = "typing";
    typing.textContent = "Assistant is writing";
    answer.appendChild(typing);
    var params = new URLSearchParams(new FormData(form));
    turnFailed = false;
    box.value = "";
    grow();
    fetch(form.action, {
      method: "POST",
      headers: {"Accept": "text/event-stream", "Content-Type": "application/x-www-form-urlencoded"},
      body: params.toString(),
      credentials: "same-origin"
    }).then(function (response) {
      if (!response.ok || !response.body) {
        typing.remove();
        answer.textContent = "The request was rejected.";
        return;
      }
      var reader = response.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";
      var raw = "";
      function pump() {
        return reader.read().then(function (chunk) {
          if (chunk.done) {
            typing.remove();
            if (!turnFailed && !String(raw || "").trim() && !answer.querySelector("pre, ul, p, a")) {
              answer.textContent = ["The turn ended with no answer.", "", "Error code: chat_empty", "Hint: Reload the chat. If it is still empty, run ssios vm-diagnose."].join("\n");
            } else if (!turnFailed) renderMarkdown(answer, raw);
            return;
          }
          buffer += decoder.decode(chunk.value, {stream: true});
          var parts = buffer.split("\n\n");
          buffer = parts.pop();
          parts.forEach(function (part) { raw = applyEvent(answer, raw, part) || raw; });
          return pump();
        });
      }
      return pump();
    }).catch(function () {
      typing.remove();
      if (!answer.textContent) answer.textContent = ["The stream stopped.", "", "Error code: chat_failed", "Hint: Reload the chat. If it is still empty, run ssios vm-diagnose."].join("\n");
    });
  });

  function grow() {
    box.style.height = "auto";
    box.style.height = Math.min(box.scrollHeight, 160) + "px";
  }

  function renderMessage(message) {
    var role = message.role === "user" ? "user" : "assistant";
    var bubble = document.createElement("div");
    bubble.className = "bubble " + role;
    if (role === "assistant") renderMarkdown(bubble, message.text || "");
    else bubble.textContent = message.text || "";
    column.appendChild(bubble);
    if (message.route) {
      var badge = document.createElement("button");
      badge.type = "button";
      badge.className = "badge";
      badge.textContent = message.route;
      badge.title = message.reason || message.route;
      badge.setAttribute("aria-label", "Route " + message.route + ". " + (message.reason || ""));
      column.appendChild(badge);
    }
    if (message.approval) addApproval(message.approval);
    log.scrollTop = log.scrollHeight;
    return bubble;
  }

  function renderMarkdown(node, text) {
    node.textContent = "";
    String(text || "").split(/```/).forEach(function (part, index) {
      if (index % 2 === 1) {
        var pre = document.createElement("pre");
        var code = document.createElement("code");
        code.textContent = part.replace(/^\w+\n/, "");
        pre.appendChild(code);
        node.appendChild(pre);
        return;
      }
      part.split(/\n{2,}/).forEach(function (block) {
        var lines = block.split("\n");
        if (lines.every(function (line) { return /^\s*[-*] /.test(line); })) {
          var list = document.createElement("ul");
          lines.forEach(function (line) { var li = document.createElement("li"); appendInline(li, line.replace(/^\s*[-*] /, "")); list.appendChild(li); });
          node.appendChild(list);
          return;
        }
        if (!block.trim()) return;
        var para = document.createElement("p");
        appendInline(para, block);
        node.appendChild(para);
      });
    });
  }

  function appendInline(node, text) {
    var rest = String(text || "");
    var pattern = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\[[^\]]+\]\([^)]+\))/;
    while (rest) {
      var match = pattern.exec(rest);
      if (!match) { node.appendChild(document.createTextNode(rest)); break; }
      if (match.index) node.appendChild(document.createTextNode(rest.slice(0, match.index)));
      var token = match[0];
      if (token.charAt(0) === "`") {
        var code = document.createElement("code");
        code.textContent = token.slice(1, -1);
        node.appendChild(code);
      } else if (token.slice(0, 2) === "**") {
        var strong = document.createElement("strong");
        strong.textContent = token.slice(2, -2);
        node.appendChild(strong);
      } else {
        var label = token.slice(1, token.indexOf("]"));
        var url = token.slice(token.indexOf("(") + 1, -1);
        var safe = safeUrl(url);
        if (safe) {
          var link = document.createElement("a");
          link.href = safe;
          link.rel = "noopener noreferrer";
          link.textContent = label;
          node.appendChild(link);
        } else node.appendChild(document.createTextNode(label));
      }
      rest = rest.slice(match.index + token.length);
    }
  }

  function safeUrl(url) {
    try {
      var parsed = new URL(url, "https://invalid.local");
      if (parsed.protocol === "http:" || parsed.protocol === "https:" || parsed.protocol === "mailto:") {
        if (parsed.hostname === "invalid.local") return "";
        return parsed.href;
      }
    } catch (err) { return ""; }
    return "";
  }

  function applyEvent(answer, raw, part) {
    var name = "message";
    var data = "";
    part.split("\n").forEach(function (line) {
      if (line.indexOf("event:") === 0) name = line.slice(6).trim();
      if (line.indexOf("data:") === 0) data += line.slice(5).trim();
    });
    if (!data) return raw;
    var payload = {};
    try { payload = JSON.parse(data); } catch (err) { return raw; }
    if (name === "token") return raw + (payload.text || "");
    if (name === "route") {
      var badge = document.createElement("button");
      badge.type = "button";
      badge.className = "badge";
      badge.textContent = payload.route || "route";
      badge.title = payload.reason || "";
      column.appendChild(badge);
    }
    if (name === "approval") addApproval({id: payload.id, status: "pending", actionable: true, preview: payload.preview || "", destructive: !!payload.destructive, action: payload.action, payload_sha256: payload.payload_sha256, expected_revision: payload.expected_revision, bound_hash: payload.bound_hash});
    if (name === "error") {
      turnFailed = true;
      answer.textContent = payload.detail || "The turn failed.";
    }
    if (name === "conversation" && payload.id) {
      var hidden = form.querySelector("input[name=conversation]");
      if (hidden) hidden.value = payload.id;
    }
    if (name === "outcome" || name === "status") {
      var note = document.createElement("p");
      note.textContent = name === "outcome" ? (payload.text || "") : ("Guest: " + (payload.phase || ""));
      column.appendChild(note);
    }
    return raw;
  }

  function addApproval(payload) {
    var card = document.createElement("article");
    card.className = payload.destructive ? "card danger" : "card";
    var state = document.createElement("p");
    state.textContent = payload.status || "pending";
    card.appendChild(state);
    var pre = document.createElement("pre");
    pre.textContent = payload.preview || "";
    card.appendChild(pre);
    if (payload.actionable) {
      var approval = document.createElement("form");
      approval.method = "post";
      approval.action = "/ui/approvals/" + encodeURIComponent(payload.id || "");
      [["csrf", csrf], ["action", payload.action], ["payload_sha256", payload.payload_sha256], ["expected_revision", payload.expected_revision], ["bound_hash", payload.bound_hash]].forEach(function (pair) {
        var input = document.createElement("input");
        input.type = "hidden";
        input.name = pair[0];
        input.value = pair[1] || "";
        approval.appendChild(input);
      });
      [["approve", "Approve"], ["deny", "Deny"]].forEach(function (pair) {
        var button = document.createElement("button");
        button.type = "submit";
        button.name = "decision";
        button.value = pair[0];
        button.textContent = pair[1];
        approval.appendChild(button);
      });
      card.appendChild(approval);
    }
    column.appendChild(card);
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
    if path == "/ui/chat/rename" and method == "POST":
        _csrf(session, body)
        return _chat_rename(plane, session, body)
    if path == "/ui/chat/delete" and method == "POST":
        _csrf(session, body)
        return _chat_delete(plane, session, body)
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
    status = 200
    for event in events:
        if event.get("event") == "error":
            status = int(event.get("data", {}).get("status") or 500)
    return Page(status, _page(session, "Chat", _events_html(session, events)))


def _sse(plane, account, text, override, peer_ip, conversation):
    try:
        for event in iter_turn(plane, account, text, override, peer_ip, conversation):
            yield _frame(event)
    except SsiosError as exc:
        from ssios.chat import english_connector_error, failure_hint

        detail = english_connector_error(exc)
        hint = failure_hint(exc.code)
        text = f"{detail}\n\nError code: {exc.code}\nHint: {hint}"
        yield _frame({"event": "error", "data": {"code": exc.code, "detail": text, "hint": hint}})
    except Exception as exc:
        from ssios.chat import failure_hint

        code = "chat_failed"
        text = f"The turn stopped because of {exc.__class__.__name__}.\n\nError code: {code}\nHint: {failure_hint(code)}"
        yield _frame({"event": "error", "data": {"code": code, "detail": text, "hint": failure_hint(code)}})


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


def _transcript(plane, account: dict, record: dict) -> list[dict]:
    messages = []
    for message in record.get("messages") or []:
        route = message.get("route") if isinstance(message.get("route"), dict) else {}
        item = {
            "role": str(message.get("role") or ""),
            "text": str(message.get("text") or ""),
            "route": str(route.get("route") or ""),
            "reason": str(route.get("reason") or ""),
        }
        if message.get("approval_id"):
            item["approval"] = _approval_view(plane, account, str(message["approval_id"]), item["text"])
        messages.append(item)
    return messages


def _approval_view(plane, account: dict, approval_id: str, preview: str) -> dict:
    import time

    record = _load_own_approval(plane, account, approval_id)
    destructive = preview.startswith("DESTRUCTIVE")
    if record is None:
        return {"id": approval_id, "status": "missing", "actionable": False, "preview": preview, "destructive": destructive}
    status = str(record.get("status") or "pending")
    try:
        expires = float(record.get("expires_at") or 0)
    except (TypeError, ValueError):
        expires = 0
    if status == "pending" and expires and expires <= time.time():
        status = "expired"
    shown = "approved" if status == "consumed" else status
    view = {"id": approval_id, "status": shown, "actionable": shown == "pending", "preview": preview, "destructive": destructive}
    if view["actionable"]:
        view.update(
            {
                "action": str(record.get("action") or ""),
                "payload_sha256": str(record.get("payload_sha256") or ""),
                "expected_revision": str(record.get("expected_revision") or ""),
                "bound_hash": str(record.get("bound_hash") or ""),
            }
        )
    return view


def _load_own_approval(plane, account: dict, approval_id: str):
    try:
        record = plane.approvals.get(approval_id)
    except SsiosError:
        record = None
    if isinstance(record, dict) and record.get("action") in ("connector.fetch", "connector.write"):
        google = getattr(plane, "_google", None)
        material = google._requests.get(approval_id) if google is not None else None
        applied = google._applied.get(approval_id) if google is not None else None
        owner = material.get("account_id") if isinstance(material, dict) else applied
        if owner != account.get("id"):
            return None
        return record
    scope = AccountScope(plane, account)
    if scope.has_approval(approval_id):
        return scope.approvals().get(approval_id)
    if isinstance(record, dict) and account.get("legacy_owner"):
        return record
    return None


def _chat_rename(plane, session, body) -> "Page":
    from ssios.ui import Page

    scope = AccountScope(plane, session["account"])
    conv = str(body.get("conversation") or "")
    scope.chat().rename(conv, str(body.get("title") or ""))
    return Page(303, "", location="/ui/chat?conversation=" + conv)


def _chat_delete(plane, session, body) -> "Page":
    from ssios.ui import Page

    scope = AccountScope(plane, session["account"])
    scope.chat().delete(str(body.get("conversation") or ""))
    return Page(303, "", location="/ui/chat")


def _chat_page(plane, session, conversation_id: str) -> str:
    from ssios.ui import _page

    scope = AccountScope(plane, session["account"])
    rows = []
    csrf = html.escape(str(session.get("csrf") or ""))
    conversations = sorted(scope.chat().list_conversations(), key=lambda item: float(item.get("updated_at") or 0), reverse=True)
    for item in conversations:
        conv_id = html.escape(str(item["id"]))
        title = html.escape(str(item["title"]))
        rows.append(
            "<li><a href=\"/ui/chat?conversation=" + conv_id + "\">" + title + "</a>"
            + f"<form method=\"post\" action=\"/ui/chat/rename\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
            + f"<input type=\"hidden\" name=\"conversation\" value=\"{conv_id}\">"
            + f"<label>Rename <input name=\"title\" value=\"{title}\"></label><button type=\"submit\">Rename</button></form>"
            + f"<form method=\"post\" action=\"/ui/chat/delete\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
            + f"<input type=\"hidden\" name=\"conversation\" value=\"{conv_id}\"><button type=\"submit\">Delete</button></form></li>"
        )
    record = None
    if conversation_id:
        try:
            record = scope.chat().get(conversation_id)
        except SsiosError:
            record = None
    state = {
        "conversation": record["id"] if record else "",
        "messages": _transcript(plane, session["account"], record) if record else [],
    }
    blob = json.dumps(state, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    conv = html.escape(state["conversation"])
    body = (
        "<div class=\"app\"><div class=\"shell\">"
        "<aside id=\"sidebar\" class=\"sidebar\"><a href=\"/ui/chat\">New chat</a><ul>" + "".join(rows) + "</ul></aside>"
        "<section class=\"thread\" aria-label=\"Conversation\">"
        "<div id=\"chat-log\" class=\"log\" aria-live=\"polite\"><div class=\"column\"></div></div>"
        "<div class=\"composer\"><form id=\"chat-form\" method=\"post\" action=\"/ui/chat/send\">"
        f"<input type=\"hidden\" name=\"csrf\" value=\"{csrf}\"><input type=\"hidden\" name=\"conversation\" value=\"{conv}\">"
        "<label class=\"grow\">Message <textarea name=\"message\" rows=\"1\" required></textarea></label>"
        "<label>Route <select name=\"override\"><option value=\"auto\">Auto</option>"
        "<option value=\"force_direct\">Force direct</option><option value=\"force_vm\">Force virtual machine</option></select></label>"
        "<button type=\"submit\">Send</button></form></div></section></div></div>"
        + "<script type=\"application/json\" id=\"chat-data\">" + blob + "</script>"
        + "<script src=\"/ui/assets/chat.js\"></script>"
        + "<noscript><p>The form still sends the message when the script does not run.</p></noscript>"
    )
    return _page(session, "Chat", body, chat=True)


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


