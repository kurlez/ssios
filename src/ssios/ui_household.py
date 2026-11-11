"""Chat, connections, sign-up, and the admin console.

These pages require a household session. The admin console lists accounts and
connection flags. It does not read another account's mail, memory, or tokens.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import time

from ssios.chat import iter_turn
from ssios.errors import SsiosError
from ssios.scope import AccountScope

CHAT_JS = r"""
(function () {
  var form = document.getElementById("chat-form");
  var log = document.getElementById("chat-log");
  var column = log ? log.querySelector(".column") : null;
  var dataNode = document.getElementById("chat-data");
  var empty = document.getElementById("empty");
  if (!form || !column) return;
  var csrf = (form.querySelector("input[name=csrf]") || {}).value || "";
  var turnFailed = false;
  var box = form.querySelector("textarea");
  var send = form.querySelector(".send");
  var state = {};
  try { state = JSON.parse(dataNode ? dataNode.textContent || "{}" : "{}"); } catch (err) { state = {}; }
  var lastRoute = {route: "", reason: ""};
  (state.messages || []).forEach(function (message) {
    if (message.role === "user" && message.route) lastRoute = {route: message.route, reason: message.reason || ""};
    if (message.role !== "user" && !message.route && lastRoute.route) {
      message = Object.assign({}, message, lastRoute);
    }
    renderMessage(message);
  });
  if ((state.messages || []).length) column.classList.add("has-messages");
  if (empty) empty.hidden = column.classList.contains("has-messages");
  document.querySelectorAll("[data-suggest]").forEach(function (button) {
    button.addEventListener("click", function () {
      box.value = button.getAttribute("data-suggest") || "";
      grow();
      syncSend();
      if (box.value.trim()) form.requestSubmit();
    });
  });
  box.addEventListener("input", function () { grow(); syncSend(); });
  box.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (box.value.trim()) {
        send.disabled = false;
        form.requestSubmit();
      }
    }
  });
  syncSend();
  var toggle = document.getElementById("sidebar-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var narrow = window.matchMedia("(max-width: 800px)").matches;
      if (narrow) {
        var open = document.body.classList.toggle("sidebar-open");
        toggle.setAttribute("aria-expanded", String(open));
      } else {
        var closed = document.body.classList.toggle("sidebar-closed");
        toggle.setAttribute("aria-expanded", String(!closed));
      }
    });
  }
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var text = box.value;
    if (!text.trim()) return;
    column.classList.add("has-messages");
    if (empty) empty.hidden = true;
    renderMessage({role: "user", text: text, at: Date.now() / 1000});
    var answer = renderMessage({role: "assistant", text: "", at: Date.now() / 1000});
    var typing = document.createElement("p");
    typing.className = "typing";
    typing.textContent = "Assistant is writing";
    answer.querySelector(".content").appendChild(typing);
    var params = new URLSearchParams(new FormData(form));
    turnFailed = false;
    box.value = "";
    grow();
    syncSend();
    var steps = null;
    fetch(form.action, {
      method: "POST",
      headers: {"Accept": "text/event-stream", "Content-Type": "application/x-www-form-urlencoded"},
      body: params.toString(),
      credentials: "same-origin"
    }).then(function (response) {
      if (!response.ok || !response.body) {
        typing.remove();
        showError(answer.querySelector(".content"), "The request was rejected.\n\nError code: chat_rejected\nHint: Reload the page and try again.");
        return;
      }
      var reader = response.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";
      var raw = "";
      var sawResult = false;
      function pump() {
        return reader.read().then(function (chunk) {
          if (chunk.done) {
            typing.remove();
            if (!turnFailed && !String(raw || "").trim() && !sawResult) {
              showError(answer.querySelector(".content"), "The turn ended with no answer.\n\nError code: chat_empty\nHint: Reload the chat. If it is still empty, run ssios vm-diagnose.");
            } else if (!turnFailed && String(raw || "").trim()) renderMarkdown(answer.querySelector(".content"), raw);
            return;
          }
          buffer += decoder.decode(chunk.value, {stream: true});
          var parts = buffer.split("\n\n");
          buffer = parts.pop();
          parts.forEach(function (part) {
            var applied = applyEvent(answer, raw, part, stepsHolder, function () { sawResult = true; });
            raw = applied == null ? raw : applied;
          });
          return pump();
        });
      }
      var stepsHolder = {node: steps, set: function (node) { steps = node; this.node = node; }};
      return pump();
    }).catch(function () {
      typing.remove();
      var content = answer.querySelector(".content");
      if (!content.textContent) showError(content, "The stream stopped.\n\nError code: chat_failed\nHint: Reload the chat. If it is still empty, run ssios vm-diagnose.");
    });
  });

  function syncSend() { if (send) send.disabled = !box.value.trim(); }
  function grow() {
    box.style.height = "auto";
    box.style.height = Math.min(box.scrollHeight, 160) + "px";
  }

  function renderMessage(message) {
    var role = message.role === "user" ? "user" : "assistant";
    var wrap = document.createElement("div");
    wrap.className = "msg " + role;
    var content;
    if (role === "user") {
      var bubble = document.createElement("div");
      bubble.className = "bubble";
      bubble.textContent = message.text || "";
      wrap.appendChild(bubble);
      content = bubble;
    } else {
      var mark = document.createElement("span");
      mark.className = "mark";
      mark.setAttribute("aria-hidden", "true");
      mark.textContent = "S";
      var body = document.createElement("div");
      body.className = "body";
      content = document.createElement("div");
      content.className = "content";
      if (message.results && message.results.length) renderResults(message.results, "", content);
      else if (message.error_code || String(message.text || "").indexOf("Error code:") >= 0) showError(content, message.text || "");
      else renderMarkdown(content, message.text || "");
      body.appendChild(content);
      var tools = document.createElement("div");
      tools.className = "tools";
      var copy = document.createElement("button");
      copy.type = "button";
      copy.className = "copy";
      copy.textContent = "Copy";
      copy.addEventListener("click", function () { copyText(message.text || content.innerText || ""); });
      tools.appendChild(copy);
      if (message.at) tools.appendChild(stamp(message.at));
      body.appendChild(tools);
      wrap.appendChild(mark);
      wrap.appendChild(body);
    }
    if (role === "user" && message.at) wrap.appendChild(stamp(message.at));
    column.appendChild(wrap);
    if (role !== "user" && message.route) pill(message.route, message.reason || "");
    if (message.checks) stepsFor(message.checks, true);
    if (message.approval) addApproval(message.approval);
    log.scrollTop = log.scrollHeight;
    return wrap;
  }

  function stamp(at) {
    var time = document.createElement("time");
    time.className = "stamp";
    time.dateTime = new Date(at * 1000).toISOString();
    time.textContent = new Date(at * 1000).toLocaleString();
    return time;
  }

  function renderResults(blocks, summary, host) {
    var target = host || column;
    var list = blocks || [];
    list.forEach(function (block) {
      var section = document.createElement("section");
      section.className = "result-block";
      var head = document.createElement("h3");
      head.textContent = block.summary || block.header || "";
      section.appendChild(head);
      if (block.note) {
        var note = document.createElement("p");
        note.className = "hint";
        note.textContent = block.note;
        section.appendChild(note);
      }
      (block.events || []).forEach(function (item) {
        var card = document.createElement("article");
        card.className = "event-card";
        if (item.badge) {
          var badge = document.createElement("p");
          badge.className = "type-badge";
          badge.textContent = item.badge;
          card.appendChild(badge);
        }
        var title = document.createElement("h4");
        title.textContent = item.title || "";
        card.appendChild(title);
        if (item.when) {
          var when = document.createElement("p");
          when.textContent = item.when;
          card.appendChild(when);
        }
        if (item.location) {
          var place = document.createElement("p");
          place.textContent = item.location;
          card.appendChild(place);
        }
        if (item.meta) {
          var meta = document.createElement("p");
          meta.textContent = item.meta;
          card.appendChild(meta);
        }
        if (item.link && /^https?:\/\//.test(item.link)) {
          var open = document.createElement("p");
          var anchor = document.createElement("a");
          anchor.href = item.link;
          anchor.rel = "noopener noreferrer";
          anchor.textContent = "Open";
          open.appendChild(anchor);
          card.appendChild(open);
        }
        if (item.description) {
          var details = document.createElement("details");
          var label = document.createElement("summary");
          label.textContent = "Details";
          details.appendChild(label);
          var body = document.createElement("p");
          linkifyInto(body, item.description);
          details.appendChild(body);
          card.appendChild(details);
        }
        section.appendChild(card);
      });
      target.appendChild(section);
    });
    if (summary && !list.length) {
      var line = document.createElement("p");
      line.textContent = summary;
      target.appendChild(line);
    }
  }

  function linkifyInto(node, text) {
    String(text || "").split(/(https?:\/\/[^\s<>\"]+)/).forEach(function (part) {
      if (part.indexOf("http://") === 0 || part.indexOf("https://") === 0) {
        var link = document.createElement("a");
        link.href = part;
        link.rel = "noopener noreferrer";
        link.textContent = part;
        node.appendChild(link);
      } else if (part) {
        node.appendChild(document.createTextNode(part));
      }
    });
  }

  function pill(route, reason) {
    var names = {direct: "Local", vm: "VM", connector_vm: "VM", connector: "Google", memory: "Memory"};
    var button = document.createElement("button");
    button.type = "button";
    button.className = "pill";
    button.textContent = names[route] || route;
    button.title = reason || route;
    button.setAttribute("aria-label", "Route " + button.textContent + ". " + (reason || ""));
    column.appendChild(button);
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).catch(function () {});
  }

  function showError(node, text) {
    node.textContent = "";
    var card = document.createElement("article");
    card.className = "error-card";
    var detail = [];
    var code = "";
    var hint = "";
    String(text || "").split("\n").forEach(function (line) {
      if (line.indexOf("Error code:") === 0) code = line;
      else if (line.indexOf("Hint:") === 0) hint = line;
      else if (line.trim()) detail.push(line);
    });
    var p = document.createElement("p");
    p.textContent = detail.join(" ") || "The turn failed.";
    card.appendChild(p);
    if (code) { var c = document.createElement("p"); c.className = "code"; c.textContent = code; card.appendChild(c); }
    if (hint) { var h = document.createElement("p"); h.className = "hint"; h.textContent = hint; card.appendChild(h); }
    node.appendChild(card);
  }

  function renderMarkdown(node, text) {
    node.textContent = "";
    String(text || "").split(/```/).forEach(function (part, index) {
      if (index % 2 === 1) {
        var lang = "";
        var body = part.replace(/^\n/, "");
        var nl = body.indexOf("\n");
        if (nl > 0 && /^[A-Za-z0-9_+.-]+$/.test(body.slice(0, nl))) {
          lang = body.slice(0, nl);
          body = body.slice(nl + 1);
        }
        var block = document.createElement("div");
        block.className = "codeblock";
        var head = document.createElement("div");
        head.className = "codehead";
        var label = document.createElement("span");
        label.textContent = lang || "code";
        var copy = document.createElement("button");
        copy.type = "button";
        copy.className = "copy";
        copy.textContent = "Copy";
        copy.addEventListener("click", function () { copyText(body); });
        head.appendChild(label);
        head.appendChild(copy);
        var pre = document.createElement("pre");
        var code = document.createElement("code");
        code.textContent = body;
        pre.appendChild(code);
        block.appendChild(head);
        block.appendChild(pre);
        node.appendChild(block);
        return;
      }
      part.split(/\n{2,}/).forEach(function (blockText) {
        var lines = blockText.split("\n");
        if (lines.length && lines.every(function (line) { return !line.trim() || /^\s*[-*] /.test(line); }) && lines.some(function (line) { return /^\s*[-*] /.test(line); })) {
          var list = document.createElement("ul");
          lines.forEach(function (line) {
            if (!line.trim()) return;
            var li = document.createElement("li");
            appendInline(li, line.replace(/^\s*[-*] /, ""));
            list.appendChild(li);
          });
          node.appendChild(list);
          return;
        }
        if (!blockText.trim()) return;
        var para = document.createElement("p");
        appendInline(para, blockText);
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

  function applyEvent(answer, raw, part, stepsHolder, onResult) {
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
    if (name === "route") pill(payload.route || "route", payload.reason || "");
    if (name === "status") markStep(stepsHolder, payload.phase || "");
    if (name === "isolation") markStep(stepsHolder, "checks", payload);
    if (name === "approval") addApproval({id: payload.id, status: "pending", actionable: true, preview: payload.preview || "", destructive: !!payload.destructive || String(payload.preview || "").indexOf("DESTRUCTIVE") === 0, action: payload.action, payload_sha256: payload.payload_sha256, expected_revision: payload.expected_revision, bound_hash: payload.bound_hash});
    if (name === "error") {
      turnFailed = true;
      showError(answer.querySelector(".content"), payload.detail || "The turn failed.");
    }
    if (name === "conversation" && payload.id) {
      var hidden = form.querySelector("input[name=conversation]");
      if (hidden) hidden.value = payload.id;
    }
    if (name === "results") {
      if (onResult) onResult();
      var typingNode = answer.querySelector(".typing");
      if (typingNode) typingNode.remove();
      renderResults(payload.blocks || [], "", answer.querySelector(".content"));
    }
    if (name === "outcome" && (payload.visible || payload.text)) {
      var note = document.createElement("p");
      note.textContent = payload.visible || payload.text;
      column.appendChild(note);
    }
    return raw;
  }

  function markStep(holder, phase, checks) {
    if (!holder.node) {
      holder.node = document.createElement("ol");
      holder.node.className = "steps";
      ["booting", "running", "checks"].forEach(function (name) {
        var li = document.createElement("li");
        li.dataset.step = name;
        li.textContent = name.charAt(0).toUpperCase() + name.slice(1);
        holder.node.appendChild(li);
      });
      column.appendChild(holder.node);
    }
    var order = ["booting", "running", "checks"];
    var current = phase === "isolation" ? "checks" : phase;
    var seen = false;
    order.forEach(function (name) {
      var li = holder.node.querySelector('[data-step="' + name + '"]');
      if (!li) return;
      if (name === current) { li.className = checks ? "done" : "spin"; seen = true; }
      else if (!seen) li.className = "done";
    });
    if (checks) {
      var list = document.createElement("ul");
      [["Network", JSON.stringify(checks.nic || [])], ["Outbound", String(checks.outbound)], ["Marker", String(checks.marker)], ["Sandbox denied", String(checks.sandbox_denied)], ["Outside write", String(checks.outside_write)]].forEach(function (pair) {
        var li = document.createElement("li");
        li.textContent = pair[0] + ": " + pair[1];
        list.appendChild(li);
      });
      column.appendChild(list);
    }
  }

  function stepsFor(checks) {
    var holder = {node: null};
    markStep(holder, "checks", checks);
    holder.node.querySelectorAll("li").forEach(function (li) { li.className = "done"; });
  }

  function addApproval(payload) {
    var parsed = parsePreview(payload.preview || "");
    var card = document.createElement("article");
    card.className = (payload.destructive || parsed.destructive) ? "card danger" : "card";
    var header = document.createElement("header");
    var title = document.createElement("h3");
    title.textContent = parsed.title;
    var chip = document.createElement("span");
    chip.className = "status " + (payload.status || "pending");
    chip.textContent = payload.status || "pending";
    header.appendChild(title);
    header.appendChild(chip);
    card.appendChild(header);
    if (parsed.fields.length) {
      var grid = document.createElement("dl");
      parsed.fields.forEach(function (pair) {
        var dt = document.createElement("dt");
        dt.textContent = pair[0];
        var dd = document.createElement("dd");
        dd.textContent = pair[1];
        grid.appendChild(dt);
        grid.appendChild(dd);
      });
      card.appendChild(grid);
    }
    if (parsed.before && parsed.after) {
      var diff = document.createElement("div");
      diff.className = "diff";
      parsed.after.forEach(function (pair) {
        var before = parsed.before.filter(function (item) { return item[0] === pair[0]; })[0];
        var row = document.createElement("p");
        var name = document.createElement("span");
        name.textContent = pair[0] + ": ";
        row.appendChild(name);
        if (before && before[1] !== pair[1]) {
          var old = document.createElement("s");
          old.textContent = before[1];
          var neu = document.createElement("span");
          neu.className = "add";
          neu.textContent = pair[1];
          row.appendChild(old);
          row.appendChild(document.createTextNode(" "));
          row.appendChild(neu);
        } else row.appendChild(document.createTextNode(pair[1]));
        diff.appendChild(row);
      });
      card.appendChild(diff);
    }
    if (parsed.body.length) {
      var extra = document.createElement("p");
      extra.textContent = parsed.body.join("\n");
      card.appendChild(extra);
    }
    if (payload.actionable) {
      var approval = document.createElement("form");
      approval.className = "actions";
      approval.method = "post";
      approval.action = "/ui/approvals/" + encodeURIComponent(payload.id || "");
      [["csrf", csrf], ["action", payload.action], ["payload_sha256", payload.payload_sha256], ["expected_revision", payload.expected_revision], ["bound_hash", payload.bound_hash]].forEach(function (pair) {
        var input = document.createElement("input");
        input.type = "hidden";
        input.name = pair[0];
        input.value = pair[1] || "";
        approval.appendChild(input);
      });
      var approve = document.createElement("button");
      approve.type = "submit";
      approve.name = "decision";
      approve.value = "approve";
      approve.textContent = "Approve";
      approve.className = (payload.destructive || parsed.destructive) ? "danger" : "primary";
      var deny = document.createElement("button");
      deny.type = "submit";
      deny.name = "decision";
      deny.value = "deny";
      deny.textContent = "Deny";
      approval.appendChild(approve);
      approval.appendChild(deny);
      card.appendChild(approval);
    }
    column.appendChild(card);
  }

  function parsePreview(text) {
    var lines = String(text || "").split("\n");
    var title = (lines[0] || "Approval").trim();
    var destructive = title.indexOf("DESTRUCTIVE") === 0;
    if (destructive) title = "Delete calendar event";
    else if (title.indexOf("Create event") === 0) title = "Create calendar event";
    else if (title.indexOf("Update event") === 0) title = "Update calendar event";
    else if (title.indexOf("List events") === 0) title = "List calendar events";
    else if (title.indexOf("Guest result") === 0) title = "Apply guest result";
    var fields = [];
    var before = null;
    var after = null;
    var mode = "";
    var body = [];
    lines.slice(1).forEach(function (line) {
      if (line === "Before:") { mode = "before"; before = []; return; }
      if (line === "After:") { mode = "after"; after = []; return; }
      var idx = line.indexOf(": ");
      if (idx > 0 && idx < 32) {
        var pair = [line.slice(0, idx), line.slice(idx + 2)];
        if (mode === "before") before.push(pair);
        else if (mode === "after") after.push(pair);
        else fields.push(pair);
      } else if (line.trim()) body.push(line);
    });
    return {title: title, destructive: destructive, fields: fields, before: before, after: after, body: body};
  }
})();

"""

CHAT_PATH = "/ui/assets/chat-" + hashlib.sha256(CHAT_JS.encode("utf-8")).hexdigest()[:12] + ".js"


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
    if path == CHAT_PATH and method == "GET":
        return Page(200, CHAT_JS, content_type="text/javascript; charset=utf-8", cache_control="public, max-age=31536000, immutable")
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
    if path == "/ui/connections/help" and method == "GET":
        return Page(200, _google_help_page(session))
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
    technical = []
    conversation_id = ""
    for event in events:
        name = event["event"]
        data = event["data"]
        if name == "conversation":
            conversation_id = str(data.get("id") or "")
            technical.append("<p class=\"conversation\">" + html.escape(conversation_id) + "</p>")
        elif name == "route":
            parts.append("<p class=\"route\">Route: " + html.escape(str(data.get("route"))) + " (" + html.escape(str(data.get("source"))) + "). " + html.escape(str(data.get("reason"))) + "</p>")
        elif name == "token":
            parts.append(html.escape(str(data.get("text") or "")))
        elif name == "status":
            parts.append("<p>Guest: " + html.escape(str(data.get("phase") or "")) + " / " + html.escape(str(data.get("backend") or "")) + "</p>")
        elif name == "isolation":
            technical.append("<p>Isolation: " + html.escape(json.dumps(data, ensure_ascii=False)) + "</p>")
        elif name == "results":
            parts.append("<p class=\"outcome\">" + html.escape(str(data.get("text") or "")) + "</p>")
            parts.append(_result_blocks_html(data.get("blocks") or []))
        elif name == "outcome":
            full = str(data.get("text") or "")
            visible = str(data.get("visible") or full)
            parts.append("<p class=\"outcome\">" + html.escape(visible) + "</p>")
            if full and full != visible:
                technical.append("<p>" + html.escape(full) + "</p>")
        elif name == "approval":
            parts.append(_approval_card(session, data))
        elif name == "error":
            parts.append("<p>" + html.escape(str(data.get("detail") or "")) + "</p>")
    details = ""
    if technical:
        details = "<details class=\"technical\"><summary>Technical details</summary>" + "".join(technical) + "</details>"
    back = "/ui/chat?conversation=" + html.escape(conversation_id) if conversation_id else "/ui/chat"
    return "<article class=\"turn\">" + "".join(parts) + details + "</article><p><a href=\"" + back + "\">Back to chat</a></p>"


def _result_blocks_html(blocks) -> str:
    chunks = []
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        header = html.escape(str(block.get("header") or ""))
        start = html.escape(str(block.get("start") or ""), quote=True)
        end = html.escape(str(block.get("end") or ""), quote=True)
        summary = html.escape(str(block.get("summary") or ""))
        cards = []
        for item in block.get("events") or []:
            if not isinstance(item, dict):
                continue
            title = html.escape(str(item.get("title") or ""))
            when = html.escape(str(item.get("when") or ""))
            location = html.escape(str(item.get("location") or ""))
            description = _linkify(str(item.get("description") or ""))
            meta = html.escape(str(item.get("meta") or ""))
            badge = html.escape(str(item.get("badge") or ""))
            extra = f"<p class=\"type-badge\">{badge}</p>" if badge else ""
            if when:
                extra += f"<p>{when}</p>"
            if location:
                extra += f"<p>{location}</p>"
            if meta:
                extra += f"<p>{meta}</p>"
            link = str(item.get("link") or "")
            if link.startswith("https://") or link.startswith("http://"):
                href = html.escape(link, quote=True)
                extra += f"<p><a href=\"{href}\" rel=\"noopener noreferrer\">Open</a></p>"
            if description:
                extra += f"<details><summary>Details</summary><p>{description}</p></details>"
            cards.append(f"<article class=\"event-card\"><h3>{title}</h3>{extra}</article>")
        note = html.escape(str(block.get("note") or ""))
        note_html = f"<p class=\"hint\">{note}</p>" if note else ""
        chunks.append(
            f"<section class=\"result-block\"><h2><time datetime=\"{start}\"></time><time datetime=\"{end}\"></time>{summary or header}</h2>{note_html}{''.join(cards)}</section>"
        )
    return "".join(chunks)


def _linkify(text: str) -> str:
    pieces = re.split(r"(https?://[^\s<>\"]+)", text)
    out = []
    for piece in pieces:
        if piece.startswith("http://") or piece.startswith("https://"):
            href = html.escape(piece, quote=True)
            out.append(f"<a href=\"{href}\" rel=\"noopener noreferrer\">{html.escape(piece)}</a>")
        else:
            out.append(html.escape(piece))
    return "".join(out)


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
        if message.get("at"):
            item["at"] = message.get("at")
        if message.get("error_code"):
            item["error_code"] = str(message.get("error_code"))
        checks = message.get("checks")
        if isinstance(checks, dict):
            item["checks"] = {
                "nic": [str(name) for name in checks.get("nic") or []] if isinstance(checks.get("nic"), list) else [],
                "outbound": checks.get("outbound"),
                "marker": bool(checks.get("marker")),
                "sandbox_denied": bool(checks.get("sandbox_denied")),
                "outside_write": bool(checks.get("outside_write")),
            }
        if message.get("approval_id"):
            item["approval"] = _approval_view(plane, account, str(message["approval_id"]), item["text"])
        results = message.get("results")
        if isinstance(results, list):
            item["results"] = [_public_block(block) for block in results if isinstance(block, dict)]
        messages.append(item)
    return messages


def _public_block(block: dict) -> dict:
    events = []
    for item in block.get("events") or []:
        if not isinstance(item, dict):
            continue
        events.append(
            {
                "title": str(item.get("title") or ""),
                "when": str(item.get("when") or ""),
                "location": str(item.get("location") or ""),
                "description": str(item.get("description") or ""),
                "meta": str(item.get("meta") or ""),
                "badge": str(item.get("badge") or ""),
                "link": str(item.get("link") or "") if str(item.get("link") or "").startswith(("http://", "https://")) else "",
            }
        )
    return {
        "kind": str(block.get("kind") or ""),
        "header": str(block.get("header") or ""),
        "summary": str(block.get("summary") or ""),
        "note": str(block.get("note") or ""),
        "start": str(block.get("start") or ""),
        "end": str(block.get("end") or ""),
        "events": events,
    }


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


def _conversation_nav(items: list[dict], csrf: str) -> str:
    now = time.localtime()
    today = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))
    groups = {"Today": [], "Yesterday": [], "Previous 7 days": [], "Older": []}
    for item in items:
        stamp = float(item.get("updated_at") or 0)
        if stamp >= today:
            groups["Today"].append(item)
        elif stamp >= today - 86400:
            groups["Yesterday"].append(item)
        elif stamp >= today - 7 * 86400:
            groups["Previous 7 days"].append(item)
        else:
            groups["Older"].append(item)
    blocks = []
    for name, rows in groups.items():
        if not rows:
            continue
        lis = []
        for item in rows:
            conv_id = html.escape(str(item["id"]))
            title = html.escape(str(item["title"]))
            lis.append(
                "<li class=\"conv\"><a href=\"/ui/chat?conversation=" + conv_id + "\">" + title + "</a>"
                + "<details class=\"kebab\"><summary aria-label=\"Rename or delete\">⋯</summary>"
                + f"<form method=\"post\" action=\"/ui/chat/rename\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
                + f"<input type=\"hidden\" name=\"conversation\" value=\"{conv_id}\">"
                + f"<label>Rename <input name=\"title\" value=\"{title}\"></label><button type=\"submit\">Rename</button></form>"
                + f"<form method=\"post\" action=\"/ui/chat/delete\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
                + f"<input type=\"hidden\" name=\"conversation\" value=\"{conv_id}\"><button type=\"submit\">Delete</button></form>"
                + "</details></li>"
            )
        blocks.append("<section><h2>" + name + "</h2><ul>" + "".join(lis) + "</ul></section>")
    return "<nav class=\"convs\" aria-label=\"Conversations\">" + "".join(blocks) + "</nav>" if blocks else ""


def _chat_page(plane, session, conversation_id: str) -> str:
    from ssios.ui import _page

    scope = AccountScope(plane, session["account"])
    csrf = html.escape(str(session.get("csrf") or ""))
    conversations = sorted(scope.chat().list_conversations(), key=lambda item: float(item.get("updated_at") or 0), reverse=True)
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
    suggestions = (
        "What's on my calendar this week?",
        "Summarize my recent emails",
        "Edit a file in my workspace",
        "What can you do on this Mac?",
    )
    chips = "".join(
        f"<button type=\"button\" class=\"chip\" data-suggest=\"{html.escape(text)}\">{html.escape(text)}</button>"
        for text in suggestions
    )
    body = (
        "<section class=\"thread\" aria-label=\"Conversation\">"
        "<div id=\"chat-log\" class=\"log\"><div class=\"column\">"
        "<div class=\"empty\" id=\"empty\"><span class=\"mark\" aria-hidden=\"true\">S</span>"
        "<h1>How can I help?</h1><p>Ask about your calendar, mail, or a file on this Mac.</p>"
        "<div class=\"chips\">" + chips + "</div></div></div></div>"
        "<div class=\"composer\"><form id=\"chat-form\" class=\"composer-box\" method=\"post\" action=\"/ui/chat/send\">"
        f"<input type=\"hidden\" name=\"csrf\" value=\"{csrf}\"><input type=\"hidden\" name=\"conversation\" value=\"{conv}\">"
        "<textarea name=\"message\" rows=\"1\" aria-label=\"Message\" placeholder=\"Message SSIOS…\" required></textarea>"
        "<div class=\"composer-bar\"><label class=\"route-chip\"><select name=\"override\" aria-label=\"Route\">"
        "<option value=\"auto\">Auto</option><option value=\"force_direct\">Local</option>"
        "<option value=\"force_vm\">VM</option></select></label>"
        "<button type=\"submit\" class=\"send primary\" aria-label=\"Send\">"
        "<svg viewBox=\"0 0 24 24\" width=\"18\" height=\"18\" aria-hidden=\"true\"><path fill=\"currentColor\" d=\"M3 11.5 21 3l-8.5 18-2.2-7.3z\"/></svg>"
        "</button></div></form>"
        "<p class=\"hint\">Enter to send, Shift+Enter for a new line</p></div></section>"
        + "<script type=\"application/json\" id=\"chat-data\">" + blob + "</script>"
        + "<script src=\"" + CHAT_PATH + "\"></script>"
        + "<noscript><p>The form still sends the message when the script does not run.</p></noscript>"
    )
    return _page(session, "Chat", body, chat=True, conversations=_conversation_nav(conversations, csrf))


def _connection_view(plane, account: dict) -> dict:
    from ssios.google_connector import session_for_account

    return plane.google.status(session_for_account(account))


def _google_help_page(session) -> str:
    from ssios.ui import _page

    body = (
        "<p>Create the client in Google Cloud, then paste it on the Connections page. "
        "The secret stays in the Keychain.</p>"
        "<ol class=\"help\">"
        "<li>Enable the <a href=\"https://console.cloud.google.com/apis/library/gmail.googleapis.com\" target=\"_blank\" rel=\"noopener noreferrer\">Gmail API</a>, "
        "the <a href=\"https://console.cloud.google.com/apis/library/drive.googleapis.com\" target=\"_blank\" rel=\"noopener noreferrer\">Google Drive API</a>, and "
        "the <a href=\"https://console.cloud.google.com/apis/library/calendar-json.googleapis.com\" target=\"_blank\" rel=\"noopener noreferrer\">Google Calendar API</a>. "
        "A disabled API is reported by Google as a permissions error.</li>"
        "<li>On Data Access, add gmail.readonly, drive.readonly, and calendar.events. "
        "calendar.calendarlist.readonly is optional. Do not add the broad calendar scope.</li>"
        "<li>Leave the project in Testing and add only your test user. Create a Desktop OAuth client.</li>"
        "<li>Download the Desktop client JSON, or copy the client id and secret into the form. Do not put the secret in the config file.</li></ol>"
        "<p><a href=\"/ui/connections\">Back to Connections</a></p>"
    )
    return _page(session, "Google Cloud client", body)


def _connections_page(plane, session, notice: str, peer_ip: str = "127.0.0.1", focus: str = "") -> str:
    from ssios.config import connector_config_location
    from ssios.netguard import is_loopback
    from ssios.google_connector import session_for_account
    from ssios.ui import CONN_PATH, _page

    account = session["account"]
    own = session_for_account(account)
    try:
        status = plane.google.status(own)
        client = plane.google.client_public(own)
    except SsiosError as exc:
        from ssios.chat import public_error

        status = {"connectors": {}}
        client = {"ready": False, "client_id_tail": ""}
        if not notice:
            notice = f"<p class=\"notice\">{html.escape(public_error(exc))}</p>"
    connectors = status.get("connectors") if isinstance(status.get("connectors"), dict) else {}
    csrf = html.escape(str(session.get("csrf") or ""))
    ready = bool(client.get("ready"))
    remote = not is_loopback(peer_ip)
    any_on = False
    any_linked = False
    cards = []
    for name in ("gmail", "drive", "calendar"):
        item = connectors.get(name) if isinstance(connectors.get(name), dict) else {}
        connected = bool(item.get("connected"))
        any_linked = any_linked or connected
        gate = plane.google.service_gate(account["id"], name)
        if gate == "on":
            any_on = True
        state = "Connected" if connected else "Not connected"
        card_notice = notice if focus == name else ""
        try:
            limit_note = plane.google.service_note(own, name)
        except SsiosError:
            limit_note = ""
        if gate == "config":
            where = connector_config_location(plane.settings)
            reason = f"Disabled in config by the administrator. Edit {where} and add \"{name}\" to enabled."
            switch = ""
        elif gate == "account":
            reason = "Turn on this service to connect it."
            switch = (
                f"<form method=\"post\" action=\"/ui/connections\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
                f"<input type=\"hidden\" name=\"service\" value=\"{name}\">"
                "<button name=\"op\" value=\"enable\" type=\"submit\">Turn on</button></form>"
            )
        else:
            reason = ""
            switch = (
                f"<form method=\"post\" action=\"/ui/connections\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
                f"<input type=\"hidden\" name=\"service\" value=\"{name}\">"
                "<button name=\"op\" value=\"disable\" type=\"submit\">Turn off</button></form>"
            )
        consent = ""
        if connected:
            button = (
                f"<form method=\"post\" action=\"/ui/connections\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
                f"<input type=\"hidden\" name=\"service\" value=\"{name}\">"
                f"<button class=\"primary\" name=\"op\" value=\"disconnect\" type=\"submit\">Disconnect {name}</button></form>"
            )
        elif gate != "on" or not ready:
            why = reason or "Set up the Google client first."
            button = f"<p>{html.escape(why)}</p><button class=\"primary\" type=\"button\" disabled>Connect {name}</button>"
        else:
            button = (
                f"<form method=\"post\" action=\"/ui/connections\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
                f"<input type=\"hidden\" name=\"service\" value=\"{name}\">"
                f"<button class=\"primary\" name=\"op\" value=\"connect\" type=\"submit\">Connect {name}</button></form>"
            )
            try:
                consent_url = plane.google.pending_consent(own, name)
            except SsiosError:
                consent_url = ""
            if consent_url or remote:
                consent = _consent_box(name, csrf, consent_url, remote)
        dot = "dot on" if connected else "dot"
        cards.append(
            "<article class=\"svc\"><span class=\"" + dot + "\"></span><div class=\"body\">"
            + "<div class=\"svc-head\"><div><h2>" + html.escape(name.title()) + "</h2>"
            "<p>" + html.escape(state) + "</p></div><div class=\"actions\">" + switch + button + "</div></div>"
            + (f"<p class=\"hint\">{html.escape(limit_note)}</p>" if limit_note else "")
            + card_notice
            + consent
            + "</div></article>"
        )
    steps = (
        "<p class=\"hint\">Enable the "
        "<a href=\"https://console.cloud.google.com/apis/library/gmail.googleapis.com\" target=\"_blank\" rel=\"noopener noreferrer\">Gmail API</a>, the "
        "<a href=\"https://console.cloud.google.com/apis/library/drive.googleapis.com\" target=\"_blank\" rel=\"noopener noreferrer\">Google Drive API</a>, and the "
        "<a href=\"https://console.cloud.google.com/apis/library/calendar-json.googleapis.com\" target=\"_blank\" rel=\"noopener noreferrer\">Google Calendar API</a> "
        "in the Cloud project before you connect. A disabled API is reported by Google as a permissions error.</p>"
        "<ol class=\"setup-steps\">"
        + ("<li class=\"done\">1 Set up the client</li>" if ready else "<li>1 Set up the client</li>")
        + ("<li class=\"done\">2 Enable services</li>" if any_on else "<li>2 Enable services</li>")
        + ("<li class=\"done\">3 Connect</li>" if any_linked else "<li>3 Connect</li>")
        + "</ol>"
    )
    tail = html.escape(str(client.get("client_id_tail") or ""))
    client_notice = notice if not focus else ""
    fields = (
        f"<form method=\"post\" action=\"/ui/connections\" enctype=\"multipart/form-data\">"
        f"<input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        "<label>Client ID <input name=\"client_id\" autocomplete=\"off\"></label>"
        "<label>Client secret <input name=\"client_secret\" type=\"password\" autocomplete=\"off\"></label>"
        "<label>Desktop client file <input name=\"client_file\" type=\"file\" accept=\"application/json,.json\"></label>"
    )
    if ready:
        client_card = (
            "<article class=\"card\"><h2>Google OAuth client</h2>" + client_notice
            + "<p>Set up. Client id ends in " + tail + ".</p>"
            "<p><a href=\"/ui/connections/help\">Google Cloud steps</a></p>"
            "<details class=\"manage\"><summary>Manage client</summary>"
            + fields
            + "<button name=\"op\" value=\"install-client\" type=\"submit\">Replace</button></form>"
            f"<form method=\"post\" action=\"/ui/connections\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
            "<label>Type remove to delete the client and revoke its tokens <input name=\"confirm\" autocomplete=\"off\"></label>"
            "<button class=\"danger\" name=\"op\" value=\"remove-client\" type=\"submit\">Remove</button></form></details></article>"
        )
    else:
        client_card = (
            "<article class=\"card\"><h2>Google OAuth client</h2>" + client_notice + "<p>Not set up.</p>"
            "<p><a href=\"/ui/connections/help\">Google Cloud steps</a></p>"
            + fields
            + "<button class=\"primary\" name=\"op\" value=\"install-client\" type=\"submit\">Save client</button></form></article>"
        )
    script = f"<script src=\"{CONN_PATH}\"></script>"
    return _page(session, "Connections", steps + client_card + "<div class=\"svc-grid\">" + "".join(cards) + "</div>" + script)


def _consent_box(name: str, csrf: str, consent_url: str, remote: bool) -> str:
    if remote:
        hint = (
            "Open Google consent in this browser. After you approve, Google redirects to 127.0.0.1 on the Mac, "
            "and that page will not load here. Copy the full address from the address bar and paste it below. "
            "This browser is not on the Mac. The check stays on the Mac."
        )
    else:
        hint = (
            "Open Google consent. Google redirects to 127.0.0.1 on this Mac. "
            "If that page does not load, paste the full address below. The check stays on the Mac."
        )
    actions = ""
    if consent_url:
        safe = html.escape(consent_url, quote=True)
        actions = (
            "<p class=\"actions\"><a class=\"button primary\" href=\"" + safe + "\" target=\"_blank\" rel=\"noopener noreferrer\">"
            "Open Google consent</a> "
            "<button type=\"button\" data-copy=\"" + safe + "\">Copy</button></p>"
        )
    return (
        "<div class=\"consent\"><p>" + html.escape(hint) + "</p>"
        + actions
        + f"<form method=\"post\" action=\"/ui/connections\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        f"<input type=\"hidden\" name=\"service\" value=\"{name}\"><input type=\"hidden\" name=\"op\" value=\"finish\">"
        "<label>Redirected 127.0.0.1 address <input name=\"redirect_url\" autocomplete=\"off\" enterkeyhint=\"done\"></label>"
        "<button type=\"submit\">Finish consent</button></form></div>"
    )


def _connection_action(plane, own, service: str, op: str, redirect_url: str, body: dict) -> dict:
    if op == "connect":
        return plane.google.connect(own, service, open_browser=False)
    if op == "disconnect":
        return plane.google.disconnect(own, service)
    if op == "finish":
        return plane.google.submit_redirect(own, service, redirect_url)
    if op == "install-client":
        return {"client": _install_from_form(plane, own, body)}
    if op == "remove-client":
        if str(body.get("confirm") or "") != "remove":
            raise SsiosError("oauth_client_invalid", "Type remove to delete the client. Nothing was changed.", 400)
        return plane.google.remove_client(own)
    if op == "enable":
        plane.google.set_service(own, service, True)
        return {}
    if op == "disable":
        plane.google.set_service(own, service, False)
        return {}
    raise SsiosError("connector_rejected", "connection action rejected", 400)


def _install_from_form(plane, own, body: dict) -> dict:
    from ssios.google_connector import parse_desktop_client

    uploaded = str(body.get("client_file") or "").strip()
    if uploaded:
        client_id, client_secret = parse_desktop_client(uploaded)
    else:
        client_id = str(body.get("client_id") or "").strip()
        client_secret = str(body.get("client_secret") or "")
    return plane.google.install_client(own, client_id, client_secret)


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
    replacing = op == "install-client" and plane.google.client_public(own).get("ready")
    focus = service if service in {"gmail", "drive", "calendar"} and op in {"connect", "finish", "disconnect", "enable", "disable"} else ""
    try:
        result = _connection_action(plane, own, service, op, str(body.get("redirect_url") or ""), body)
    except SsiosError as exc:
        from ssios.chat import public_error

        notice = f"<p class=\"notice\">{html.escape(public_error(exc))}</p>"
        return Page(exc.status, _connections_page(plane, session, notice, peer_ip, focus=focus))
    if op == "connect":
        plane.security_audit.write(actor=account_id, ip=peer_ip, action="connector.connect", target=service, result="ok")
    elif op == "disconnect":
        plane.security_audit.write(actor=account_id, ip=peer_ip, action="connector.disconnect", target=service, result="ok")
    elif op == "finish":
        plane.security_audit.write(actor=account_id, ip=peer_ip, action="connector.connect", target=service, result="ok")
    elif op == "install-client":
        plane.security_audit.write(
            actor=account_id,
            ip=peer_ip,
            action="oauth.client.replace" if replacing else "oauth.client.install",
            target=account_id,
            result="ok",
        )
    elif op == "remove-client":
        plane.security_audit.write(actor=account_id, ip=peer_ip, action="oauth.client.remove", target=account_id, result="ok")
    elif op in {"enable", "disable"}:
        plane.security_audit.write(actor=account_id, ip=peer_ip, action="connector." + op, target=service, result="ok", extra={"service": service})
    if op == "connect":
        detail = f"<p class=\"notice ok\">Consent started for {html.escape(service.title())}.</p>"
    elif op == "finish":
        detail = f"<p class=\"notice ok\">{html.escape(service.title())} is connected.</p>"
    elif op == "install-client":
        detail = "<p>The Google client is saved in the Keychain.</p>"
    elif op == "remove-client":
        detail = "<p>The Google client was removed and its tokens were revoked.</p>"
    elif op == "enable":
        detail = "<p>Service turned on.</p>"
    elif op == "disable":
        detail = "<p>Service turned off.</p>"
    else:
        detail = "<p>Disconnected.</p>"
    return Page(200, _connections_page(plane, session, detail, peer_ip, focus=focus))


