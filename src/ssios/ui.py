"""Local web UI. The human token is checked once and is not stored in the session."""

from __future__ import annotations

import hashlib
import hmac
import html
import re
import json
import secrets
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ssios.errors import SsiosError
from ssios.ingest import _reject_secrets
from ssios.util import read_json, secure_mkdir, sha256_bytes, write_json

SESSION_COOKIE = "ssios_session"
CODE_TTL = 120
SESSION_TTL = 12 * 3600


class Page:
    def __init__(
        self,
        status: int,
        body: str,
        *,
        cookie: str | None = None,
        cookies: list[str] | None = None,
        stream=None,
        content_type: str = "text/html; charset=utf-8",
        location: str | None = None,
        cache_control: str | None = None,
        extra_headers: dict | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.cookie = cookie
        self.cookies = list(cookies or ([] if not cookie else [cookie]))
        self.stream = stream
        self.content_type = content_type
        self.location = location
        self.cache_control = cache_control
        self.extra_headers = dict(extra_headers or {})


def handle(plane, method: str, target: str, headers, body: dict, peer_ip: str = "127.0.0.1") -> Page:
    parts = urlsplit(target)
    path = parts.path.rstrip("/") or "/ui"
    query = {key: values[-1] for key, values in parse_qs(parts.query).items()}
    if path == "/ui/clip":
        return _browser_clip(plane, method, headers, body, peer_ip)
    rejected = _check_origin(plane, headers, method)
    if rejected is not None:
        return rejected
    asset = _static_asset(method, path)
    if asset is not None:
        return asset
    if method == "GET" and path == "/ui/login":
        if _session(plane, headers) is not None:
            return Page(302, "", location="/ui/chat")
        return Page(200, _login_page(query.get("code", "")))
    if method == "POST" and path == "/ui/login":
        return _login(plane, body, peer_ip, headers)
    if path in {"/ui/signup", "/ui/join"}:
        from ssios.ui_household import handle_household

        return handle_household(plane, method, path, None, body, query, peer_ip, headers)
    if (headers.get("Upgrade") or "").lower() == "websocket":
        return Page(401, _login_page(""))
    session = _session(plane, headers)
    if session is None:
        return Page(401, _login_page(""))
    from ssios.ui_household import handle_household

    extra = handle_household(plane, method, path, session, body, query, peer_ip, headers)
    if extra is not None:
        return extra
    if method == "GET" and path == "/ui":
        return Page(200, _dashboard(plane, session))
    if method == "GET" and path == "/ui/notices":
        return _notices(plane, session)
    if method == "GET" and path == "/ui/library":
        from ssios.library_page import library_page

        return Page(200, library_page(plane, session, query))
    if method == "POST" and path == "/ui/library":
        from ssios.library_page import library_post

        _csrf(session, body)
        return library_post(plane, session, body, peer_ip)
    if method == "GET" and path == "/ui/library/raw":
        from ssios.library_page import library_raw

        return library_raw(plane, session, query)
    if method == "GET" and path == "/ui/library/thumb":
        from ssios.library_page import library_thumb

        return library_thumb(plane, session, query)
    if method == "GET" and path == "/ui/tasks":
        return Page(200, _tasks(plane, session, query.get("status", "all")))
    if method == "POST" and path == "/ui/tasks":
        _csrf(session, body)
        return _tasks_post(plane, session, body, peer_ip, headers)
    if method == "GET" and path.startswith("/ui/tasks/"):
        return Page(200, _task(plane, session, path.rsplit("/", 1)[-1]))
    if method == "GET" and path == "/ui/approvals":
        return Page(200, _approvals(plane, session))
    if method == "POST" and path.startswith("/ui/approvals/"):
        _csrf(session, body)
        return _decide(plane, session, path.rsplit("/", 1)[-1], body, peer_ip)
    if method == "GET" and path == "/ui/ingest":
        return Page(200, _ingest_form(plane, session, ""))
    if method == "POST" and path == "/ui/ingest":
        _csrf(session, body)
        return _ingest(plane, session, body)
    if method == "GET" and path == "/ui/summary":
        return Page(200, _summary_form(plane, session))
    if method == "POST" and path == "/ui/summary":
        _csrf(session, body)
        return _summary_post(plane, session, body)
    if method == "GET" and path == "/ui/memory":
        return Page(200, _memory(plane, session, query.get("project_id", "local-summary"), query.get("path", ""), query.get("q", "")))
    if method == "POST" and path == "/ui/memory":
        _csrf(session, body)
        return _memory_post(plane, session, body, peer_ip)
    if method == "GET" and path == "/ui/settings":
        return Page(200, _settings(plane, session))
    if method == "POST" and path == "/ui/settings":
        _csrf(session, body)
        return _settings_post(plane, session, body)
    if method == "GET" and path == "/ui/clipper":
        return Page(200, _clipper_page(plane, session, ""))
    if method == "POST" and path == "/ui/clipper":
        _csrf(session, body)
        return _clipper_post(plane, session, body)
    if method == "POST" and path == "/ui/vault/note":
        _csrf(session, body)
        return _vault_note_post(plane, session, body, peer_ip)
    if method == "GET" and path == "/ui/vault":
        return Page(200, _vault_page(plane, session, "", str(query.get("tag") or "")))
    if method == "POST" and path == "/ui/vault":
        _csrf(session, body)
        return _vault_paste(plane, session, body, peer_ip)
    if method == "GET" and path == "/ui/vault/note":
        return Page(200, _vault_note(plane, session, str(query.get("path") or "")))
    if method == "GET" and path == "/ui/vault/asset":
        return _vault_asset(plane, session, str(query.get("path") or ""))
    if method == "POST" and path == "/ui/logout":
        _csrf(session, body)
        _drop_session(plane, session["id"])
        return Page(200, _login_page(""), cookie=f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/ui; Max-Age=0")
    return Page(404, _page(session, "Page not found", "<p>This page does not exist.</p>"))


def issue_login_code(plane) -> str:
    raw = secrets.token_urlsafe(18)
    record = {"hash": _hash(raw), "expires": time.time() + CODE_TTL}
    path = _ui_dir(plane) / "codes.json"
    current = read_json(path, {"codes": []})
    if not isinstance(current, dict):
        current = {"codes": []}
    codes = [item for item in current.get("codes") or [] if isinstance(item, dict) and item.get("expires", 0) > time.time()]
    codes.append(record)
    write_json(path, {"codes": codes}, 0o600)
    return raw


def _login(plane, body: dict, peer_ip: str, headers=None) -> Page:
    from ssios.netguard import is_loopback

    username = str(body.get("username") or "")
    password = str(body.get("password") or "")
    code = str(body.get("code") or "")
    try:
        plane.login_limiter.check(peer_ip)
    except SsiosError as exc:
        _security(plane, "anonymous", peer_ip, "login", "rate", "denied")
        return Page(exc.status, _login_page(""))
    if code and not (username and password) and is_loopback(peer_ip) and _take_code(plane, code):
        admin = plane.accounts.legacy_admin()
        if admin is None:
            plane.login_limiter.fail(peer_ip)
            _security(plane, "anonymous", peer_ip, "login", "code", "denied")
            return Page(401, _login_page(""))
        plane.login_limiter.succeed(peer_ip)
        _security(plane, admin["id"], peer_ip, "login", admin["id"], "ok")
        return _start_account_session(plane, admin, headers)
    if username and password:
        try:
            account = plane.accounts.authenticate(username, password)
        except SsiosError as exc:
            plane.login_limiter.fail(peer_ip)
            _security(plane, "anonymous", peer_ip, "login", "password", "denied")
            if exc.code == "account_inactive":
                return Page(403, _login_page(""))
            return Page(401, _login_page(""))
        plane.login_limiter.succeed(peer_ip)
        _security(plane, account["id"], peer_ip, "login", account["id"], "ok")
        return _start_account_session(plane, account, headers)
    plane.login_limiter.fail(peer_ip)
    _security(plane, "anonymous", peer_ip, "login", "password", "denied")
    return Page(401, _login_page(""))


def _start_account_session(plane, account: dict, headers=None) -> Page:
    from ssios.config import lan_enabled

    for previous in _session_ids("" if headers is None else headers.get("Cookie", "")):
        plane.accounts.drop_session(previous)
    session_id, csrf = plane.accounts.create_session(account["id"])
    fresh = plane.accounts.read_session(session_id) or {"id": session_id, "csrf": csrf, "account": account}
    cookie = f"{SESSION_COOKIE}={session_id}; HttpOnly; SameSite=Strict; Path=/ui"
    if lan_enabled(plane.settings):
        cookie += "; Secure"
    cleared = f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
    return Page(
        200,
        _page(fresh, "Signed in", "<p>Signed in as " + html.escape(str(account.get("username") or "")) + ". The password was not written to the session file.</p><p><a href=\"/ui\">Open the console</a></p>"),
        cookies=[cookie, cleared],
    )


def _security(plane, actor: str, peer_ip: str, action: str, target: str, result: str) -> None:
    try:
        plane.security_audit.write(actor=actor, ip=peer_ip or "127.0.0.1", action=action, target=target, result=result)
    except SsiosError:
        return


def _session_ids(header: str) -> list[str]:
    found = []
    for part in str(header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == SESSION_COOKIE and value and value not in found:
            found.append(value)
    return found


def _session(plane, headers) -> dict | None:
    found = []
    for session_id in _session_ids(headers.get("Cookie", "")):
        account_session = plane.accounts.read_session(session_id)
        if account_session is not None:
            found.append(account_session)
    if len(found) == 1:
        return found[0]
    return None


def _csrf(session: dict, body: dict) -> None:
    if not hmac.compare_digest(str(body.get("csrf") or ""), str(session.get("csrf") or "")):
        raise SsiosError("csrf_rejected", "csrf token rejected", 403)


def _take_code(plane, raw: str) -> bool:
    path = _ui_dir(plane) / "codes.json"
    current = read_json(path, {"codes": []})
    codes = [item for item in (current.get("codes") if isinstance(current, dict) else []) or [] if isinstance(item, dict)]
    digest = _hash(raw)
    kept = []
    found = False
    for item in codes:
        if item.get("expires", 0) < time.time():
            continue
        if not found and hmac.compare_digest(str(item.get("hash") or ""), digest):
            found = True
            continue
        kept.append(item)
    write_json(path, {"codes": kept}, 0o600)
    return found


def _drop_session(plane, session_id: str) -> None:
    plane.accounts.drop_session(session_id)
    path = _ui_dir(plane) / "sessions.json"
    current = read_json(path, {"sessions": {}})
    sessions = current.get("sessions") if isinstance(current, dict) else {}
    if isinstance(sessions, dict) and session_id in sessions:
        sessions.pop(session_id, None)
        write_json(path, {"sessions": sessions}, 0o600)


def _ui_dir(plane):
    path = plane.settings.state_dir / "ui"
    secure_mkdir(path)
    return path


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _bound_port(plane) -> int:
    port = plane.bound_port or plane.settings.port
    return int(port)


def ui_language(plane, session: dict | None) -> str:
    if not session:
        return "en"
    try:
        lang = plane.skills.account_prefs(session["account"]["id"]).get("language")
    except Exception:
        return "en"
    return lang if lang in {"en", "zh"} else "en"


def browser_error_page(detail: str, status: int, *, code: str = "", lang: str = "en") -> Page:
    """A normal page for a browser. The link matches the failure, not every destination."""
    from ssios.locale_messages import say

    choice = lang if lang in {"en", "zh"} else "en"
    safe = html.escape(detail or say(choice, "page_failed"))
    if status == 401 or code in {"unauthorized", "login_required"}:
        href, label = "/ui/login", say(choice, "sign_in")
    elif code.startswith("library_"):
        href, label = "/ui/library", say(choice, "library_link")
    elif code.startswith("clip_") or code == "path_rejected":
        href, label = "/ui/vault", say(choice, "notes_link")
    elif code.startswith("connector") or code.startswith("oauth") or code == "secure_store_unavailable":
        href, label = "/ui/connections", say(choice, "connections_link")
    else:
        href, label = "/ui/chat", say(choice, "chat_link")
    title = say(choice, "page_failed_title")
    code_html = ""
    if code:
        code_html = (
            f'<details class="error-code"><summary>{html.escape(say(choice, "error_details"))}</summary>'
            f"<p>{html.escape(code)}</p></details>"
        )
    body = (
        f"<!doctype html><html lang=\"{choice}\"><meta charset=\"utf-8\"><title>{html.escape(title)}</title>"
        + _style_link()
        + f"<body><main><h1>{html.escape(title)}</h1><p>"
        + safe
        + "</p>"
        + code_html
        + f'<p><a href="{html.escape(href, quote=True)}">{html.escape(label)}</a></p></main></body></html>'
    )
    return Page(status, body)


def loopback_origins(port: int) -> set[str]:
    return {f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"}


def _origin_value(value: str) -> str:
    text = value.strip()
    if not text or text.lower() == "null":
        return ""
    parts = urlsplit(text if "://" in text else "")
    if parts.scheme != "http" or parts.username or parts.password or parts.port is None:
        return ""
    host = parts.hostname or ""
    if host == "::1":
        host = "[::1]"
    if host not in ("127.0.0.1", "localhost", "[::1]"):
        return ""
    return f"http://{host}:{parts.port}"


def _check_origin(plane, headers, method: str = "GET") -> Page | None:
    """Reject cross-site UI posts. A literal Origin of null falls back to Referer."""
    from ssios.config import lan_enabled
    from ssios.netguard import origin_allowed

    if lan_enabled(plane.settings):
        port = _bound_port(plane)
        origin = str(headers.get("Origin") or "").strip()
        site = str(headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site == "cross-site":
            return _origin_rejection(origin or "(no Origin)", {f"https://127.0.0.1:{port}"})
        if method == "POST" or origin:
            if not origin_allowed(origin, lan=True, port=port):
                return _origin_rejection(origin or "(no Origin)", {f"https://<lan-host>:{port}"})
        return None
    port = _bound_port(plane)
    allowed = loopback_origins(port)
    origin = str(headers.get("Origin") or "").strip()
    site = str(headers.get("Sec-Fetch-Site") or "").strip().lower()
    if site == "cross-site":
        return _origin_rejection(origin or "(no Origin)", allowed)
    if origin.lower() in ("", "null"):
        referer = str(headers.get("Referer") or headers.get("Referrer") or "").strip()
        if referer and _origin_value(referer) not in allowed:
            return _origin_rejection(origin or "(no Origin)", allowed)
        return None
    if _origin_value(origin) not in allowed:
        return _origin_rejection(origin, allowed)
    return None


def _origin_rejection(received: str, allowed: set[str]) -> Page:
    shown = html.escape(received)
    expected = ", ".join(html.escape(item) for item in sorted(allowed))
    body = (
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><title>Origin rejected</title>"
        + _style_link()
        + "<body><main><h1>Origin rejected</h1>"
        + f"<p>Origin received: {shown}</p>"
        + f"<p>Allowed origins: {expected}</p>"
        + "<p>Open the login page again from http://127.0.0.1 and the actual port. Origins from other sites are rejected.</p>"
        + "</main></body></html>"
    )
    return Page(403, body)


def _human(plane) -> str:
    return plane.auth.read_secret("human")


def _dashboard(plane, session: dict) -> str:
    status = plane.status(_human(plane))
    runtime = status.get("runtime") if isinstance(status.get("runtime"), dict) else {}
    isolated = "yes" if runtime.get("isolated") else "no"
    body = (
        f"<p>Backend: {html.escape(str(runtime.get('active_backend')))}</p>"
        f"<p>Isolated: {isolated}</p>"
        f"<p>Model: {html.escape(str(status.get('llm', {}).get('provider')))} / {html.escape(str(status.get('llm', {}).get('model')))}</p>"
        f"<p>Memory: {html.escape(str(status.get('memory', {}).get('mode')))}</p>"
    )
    return _page(session, "Console", body)


def _account(session: dict) -> dict:
    account = session.get("account")
    if not isinstance(account, dict):
        raise SsiosError("unauthorized", "authentication required", 401)
    return account


def _notices(plane, session: dict) -> Page:
    from ssios.reminders import ReminderStore, sweep_accounts
    from ssios.scope import AccountScope

    sweep_accounts(plane)
    scope = AccountScope(plane, _account(session))
    from ssios.site_session import warnings

    items = [
        {"text": str(item.get("text") or "")[:160], "due": str(item.get("due") or "")}
        for item in ReminderStore(scope.home).unseen()
    ]
    for note in warnings(plane, scope.account_id):
        items.append({"text": note[:160], "due": ""})
    payload = json.dumps({"count": len(items), "items": items[:10]}, ensure_ascii=False)
    return Page(200, payload, content_type="application/json; charset=utf-8")


def task_check_control(session: dict, task: dict) -> str:
    """A square checkbox. A saved task changes when its box is ticked. Anything else is read-only."""
    title = " ".join(str(task.get("title") or "").split())
    if not title:
        return ""
    status = "done" if str(task.get("status") or "") == "done" else "open"
    due = str(task.get("due") or "")
    label = title + (f" (due {due})" if due else "")
    pressed = "true" if status == "done" else "false"
    word = "Done" if status == "done" else "Open"
    writable = bool(task.get("writable")) and bool(task.get("id"))
    if not writable:
        hint = str(task.get("hint") or "This box is read-only.")
        return (
            "<div class=\"row\"><span class=\"check\" aria-disabled=\"true\" aria-pressed=\""
            + pressed
            + "\" title=\""
            + html.escape(hint, quote=True)
            + "\"><span class=\"sr\">"
            + word
            + "</span></span><span class=\"label\">"
            + html.escape(label)
            + "</span></div>"
        )
    csrf = html.escape(str(session.get("csrf") or ""), quote=True)
    goal_id = html.escape(str(task.get("id") or ""), quote=True)
    hint = "Mark open." if status == "done" else "Mark done."
    return (
        f"<form method=\"post\" action=\"/ui/tasks\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        f"<input type=\"hidden\" name=\"id\" value=\"{goal_id}\">"
        f"<button class=\"check\" type=\"submit\" name=\"op\" value=\"toggle\" aria-pressed=\"{pressed}\" title=\"{html.escape(hint, quote=True)}\">"
        f"<span class=\"sr\">{word}</span></button><span class=\"label\">{html.escape(label)}</span></form>"
    )


def task_checklist_html(session: dict, tasks: list[dict]) -> str:
    rows = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        control = task_check_control(session, task)
        if not control:
            continue
        status = "done" if str(task.get("status") or "") == "done" else "open"
        rows.append(f"<li class=\"task {status}\">{control}</li>")
    if not rows:
        return ""
    return "<ul class=\"checklist\">" + "".join(rows) + "</ul>"


def _tasks(plane, session: dict, status_filter: str = "open", notice: str = "") -> str:
    from ssios.goals import GoalStore, goal_mode, task_view
    from ssios.reminders import ReminderStore, sweep_accounts
    from ssios.scope import AccountScope

    account = _account(session)
    scope = AccountScope(plane, account)
    sweep_accounts(plane)
    chosen = status_filter if status_filter in {"open", "done", "dropped", "in_progress", "all"} else "open"
    goals = GoalStore(scope.home).all()
    if chosen == "open":
        shown = [goal for goal in goals if goal.get("status") in {"open", "in_progress"}]
    elif chosen == "all":
        shown = goals
    else:
        shown = [goal for goal in goals if goal.get("status") == chosen]
    csrf = html.escape(str(session.get("csrf") or ""))
    filters = " ".join(
        f"<a href=\"/ui/tasks?status={name}\">{name}</a>" for name in ("open", "done", "dropped", "all")
    )
    rows = []
    for goal in shown:
        goal_id = html.escape(str(goal.get("id") or ""))
        source = goal.get("source") if isinstance(goal.get("source"), dict) else {}
        conversation = html.escape(str(source.get("conversation_id") or ""))
        link = f"<a href=\"/ui/chat?conversation={conversation}\">Source</a>" if conversation else ""
        evidence = goal.get("evidence") if isinstance(goal.get("evidence"), dict) else {}
        reason = html.escape(str(evidence.get("reason") or ""))
        buttons = (
            f"<form class=\"goal-actions\" method=\"post\" action=\"/ui/tasks\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
            f"<input type=\"hidden\" name=\"id\" value=\"{goal_id}\">"
            "<button name=\"op\" value=\"drop\" type=\"submit\">Drop</button>"
            "<button name=\"op\" value=\"undo\" type=\"submit\">Undo</button></form>"
        )
        box = task_check_control(session, task_view(goal))
        state = "done" if goal.get("status") == "done" else "open"
        rows.append(f"<li class=\"task {state}\">{box} {link} {reason} {buttons}</li>")
    mode = html.escape(goal_mode(scope.home))
    mode_form = (
        f"<form method=\"post\" action=\"/ui/tasks\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        f"<label>Goal updates <select name=\"mode\"><option {'selected' if mode=='auto' else ''}>auto</option>"
        f"<option {'selected' if mode=='suggest' else ''}>suggest</option>"
        f"<option {'selected' if mode=='off' else ''}>off</option></select></label>"
        "<button name=\"op\" value=\"mode\" type=\"submit\">Save</button></form>"
    )
    add = (
        f"<form method=\"post\" action=\"/ui/tasks\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        "<label>Title <input name=\"title\" maxlength=\"80\"></label>"
        "<label>Description <input name=\"description\" maxlength=\"240\"></label>"
        "<button name=\"op\" value=\"add\" type=\"submit\">Add goal</button></form>"
    )
    guest_rows = []
    guest_tasks = []
    if account.get("legacy_owner"):
        guest_tasks.extend(task for task in plane.tasks.values() if isinstance(task, dict))
    guest_tasks.extend(task for task in scope.tasks().values() if isinstance(task, dict))
    for task in guest_tasks:
        status = str(task.get("status") or "")
        guest_rows.append(
            {
                "id": "",
                "title": str(task.get("id") or ""),
                "status": "done" if status in {"done", "succeeded", "completed"} else "open",
                "writable": False,
                "hint": "Guest task status is not changed from this list.",
            }
        )
    guest = []
    for task in guest_tasks:
        guest.append(
            f"<li><a href=\"/ui/tasks/{html.escape(str(task.get('id') or ''))}\">{html.escape(str(task.get('id') or ''))}</a> {html.escape(str(task.get('status') or ''))}</li>"
        )
    reminder_store = ReminderStore(scope.home)
    notices = []
    for item in reminder_store.unseen():
        notices.append(f"<li>Reminder: {html.escape(str(item.get('text') or ''))} was due {html.escape(str(item.get('due') or ''))}.</li>")
    dismiss = (
        f"<form method=\"post\" action=\"/ui/tasks\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        "<button name=\"op\" value=\"dismiss\" type=\"submit\">Dismiss</button></form>"
    ) if notices else ""
    reminder_rows = []
    for item in reminder_store.open_items():
        reminder_id = html.escape(str(item.get("id") or ""))
        reminder_rows.append(
            f"<li>{html.escape(str(item.get('due') or ''))} {html.escape(str(item.get('text') or ''))} "
            f"{html.escape(str(item.get('repeat') or ''))}"
            f"<form class=\"goal-actions\" method=\"post\" action=\"/ui/tasks\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
            f"<input type=\"hidden\" name=\"id\" value=\"{reminder_id}\">"
            "<button name=\"op\" value=\"snooze\" type=\"submit\">Snooze</button>"
            "<button name=\"op\" value=\"cancel\" type=\"submit\">Cancel</button></form></li>"
        )
    guest_list = task_checklist_html(session, guest_rows)
    guest_links = "<ul>" + ("".join(guest) or "<li>None.</li>") + "</ul>"
    body = (
        notice
        + f"<p>{filters}</p><p>Updates: {mode}</p>" + mode_form + add
        + "<ul class=\"checklist\">" + ("".join(rows) or "<li>No goals.</li>") + "</ul>"
        + "<h2>Reminders</h2>" + (f"<ul>{''.join(notices)}</ul>" + dismiss if notices else "")
        + "<ul>" + ("".join(reminder_rows) or "<li>No open reminders.</li>") + "</ul>"
        + "<h2>Guest tasks</h2>" + guest_list + guest_links
    )
    return _page(session, "Tasks", body)


def _tasks_post(plane, session, body: dict, peer_ip: str, headers=None) -> Page:
    from ssios.calendar_intent import host_clock
    from ssios.goals import manual_goal, set_goal_mode, undo_goal
    from ssios.reminders import ReminderStore
    from ssios.scope import AccountScope

    account = _account(session)
    scope = AccountScope(plane, account)
    op = str(body.get("op") or "")
    now, _zone = host_clock(plane, account)
    if op == "mode":
        set_goal_mode(scope.home, str(body.get("mode") or "auto"))
    elif op == "undo":
        undo_goal(scope, str(body.get("id") or ""), now, peer_ip)
    elif op == "add":
        manual_goal(scope, title=str(body.get("title") or ""), description=str(body.get("description") or ""), status="open", goal_id="", now=now, peer_ip=peer_ip)
    elif op in {"complete", "drop", "edit"}:
        status = {"complete": "done", "drop": "dropped", "edit": str(body.get("status") or "open")}[op]
        manual_goal(
            scope,
            title=str(body.get("title") or "") or _goal_title(scope, str(body.get("id") or "")),
            description=str(body.get("description") or ""),
            status=status,
            goal_id=str(body.get("id") or ""),
            now=now,
            peer_ip=peer_ip,
        )
    elif op == "snooze":
        ReminderStore(scope.home).snooze_matching(str(body.get("id") or ""), 60, now)
    elif op == "cancel":
        ReminderStore(scope.home).cancel_matching(str(body.get("id") or ""))
    elif op == "dismiss":
        ReminderStore(scope.home).mark_seen()
    elif op == "toggle":
        # The account ticked its own box on a signed-in page with a CSRF token. That is the decision,
        # so the status changes now. The change is audited and Undo on the Tasks page reverses it.
        from ssios.goals import GoalStore

        goal = GoalStore(scope.home).get(str(body.get("id") or ""))
        if goal is None or goal.get("status") == "dropped":
            raise SsiosError("goal_invalid", "goal not found", 404)
        manual_goal(
            scope,
            title=str(goal.get("title") or ""),
            description=str(goal.get("description") or ""),
            status="open" if goal.get("status") == "done" else "done",
            goal_id=str(goal["id"]),
            now=now,
            peer_ip=peer_ip,
        )
        back = _chat_referer(headers)
        if back:
            return Page(303, "", location=back)
    else:
        raise SsiosError("goal_invalid", "goal action rejected", 400)
    return Page(200, _tasks(plane, session, "open"))


def _chat_referer(headers) -> str:
    """The chat page the box was ticked on, as a path on this site. Empty for any other page."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(str(headers.get("Referer") or "")) if headers is not None else None
    except (AttributeError, ValueError):
        return ""
    if parts is None or parts.path != "/ui/chat":
        return ""
    conversation = re.fullmatch(r"conversation=(conv-[0-9a-f]{16})", parts.query or "")
    return "/ui/chat" + ("?" + conversation.group(0) if conversation else "")


def _goal_title(scope, goal_id: str) -> str:
    from ssios.goals import GoalStore

    goal = GoalStore(scope.home).get(goal_id) or {}
    return str(goal.get("title") or "")


def _task(plane, session: dict, task_id: str) -> str:
    from ssios.scope import AccountScope

    account = _account(session)
    scope = AccountScope(plane, account)
    owned = scope.tasks().get(task_id)
    if isinstance(owned, dict):
        return _page(session, task_id, f"<pre>{html.escape(json.dumps(owned, ensure_ascii=False, indent=2))}</pre>")
    if not account.get("legacy_owner"):
        return _page(session, "Task", "<p>Task not found.</p>")
    try:
        task = plane.get_task(_human(plane), task_id)
    except SsiosError as exc:
        return _page(session, "Task", f"<p>{html.escape(exc.detail)}</p>")
    isolated = "yes" if task.get("isolated") else "no"
    boot = task.get("boot_ms")
    boot_line = f"{boot} ms" if isinstance(boot, int) else "none"
    summary = (
        f"<p>Backend: {html.escape(str(task.get('runtime_backend') or ''))}</p>"
        f"<p>Isolated: {isolated}</p>"
        f"<p>Boot and run: {html.escape(boot_line)}</p>"
    )
    if task.get("error_detail"):
        summary += f"<p>Error: {html.escape(str(task.get('error_detail')))}</p>"
    return _page(session, task_id, summary + f"<pre>{html.escape(json.dumps(task, ensure_ascii=False, indent=2))}</pre>")


def _approvals(plane, session: dict) -> str:
    from ssios.scope import AccountScope

    rows = []
    account = _account(session)
    scope = AccountScope(plane, account)
    records = []
    if account.get("legacy_owner"):
        records.extend(
            record
            for record in plane.approvals.records()
            if record.get("status") == "pending" and record.get("action") not in ("connector.fetch", "connector.write")
        )
    records.extend(
        record
        for record in plane.approvals.records()
        if record.get("status") == "pending"
        and record.get("action") in ("connector.fetch", "connector.write")
        and _owns_google_fetch(plane, account["id"], record)
    )
    records.extend(scope.list_pending())
    token = _human(plane)
    for record in records:
        if record.get("status") != "pending":
            continue
        if record.get("action") in ("connector.fetch", "connector.write"):
            from ssios.google_connector import session_for_account

            shown = plane.google.preview(record, session_for_account(account))
            kind = "card approval danger" if shown.startswith("DESTRUCTIVE") else "card approval"
            diff = html.escape(shown)
            rows.append(
                "<article class=\"" + kind + "\"><h2>"
                + html.escape(str(record.get("action")))
                + "</h2><pre>"
                + diff
                + "</pre>"
                + _decision_form(session, record)
                + "</article>"
            )
            continue
        if record.get("approval_id") in {item.get("approval_id") for item in scope.list_pending()}:
            diff = html.escape(scope.preview(record))
        else:
            preview = plane.get_approval(token, record["approval_id"])
            files = preview.get("files") or []
            diff = html.escape("\n\n".join(str(item.get("content") or "") for item in files))
        rows.append(
            "<article class=\"card approval\"><h2>"
            + html.escape(str(record.get("action")))
            + "</h2><pre>"
            + diff
            + "</pre>"
            + _decision_form(session, record)
            + "</article>"
        )
    return _page(session, "Approvals", "".join(rows) or "<p>Nothing is waiting for approval.</p>")


def _decision_form(session: dict, record: dict) -> str:
    csrf = html.escape(str(session["csrf"]))
    fields = "".join(
        f"<input type=\"hidden\" name=\"{html.escape(key)}\" value=\"{html.escape(str(record.get(key) or ''))}\">"
        for key in ("action", "payload_sha256", "expected_revision", "bound_hash")
    )
    return (
        f"<form method=\"post\" action=\"/ui/approvals/{html.escape(record['approval_id'])}\">"
        f"<input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">{fields}"
        "<button class=\"primary\" name=\"decision\" value=\"approve\" type=\"submit\">Approve</button>"
        "<button name=\"decision\" value=\"deny\" type=\"submit\">Deny</button></form>"
    )


def _decide(plane, session: dict, approval_id: str, body: dict, peer_ip: str = "127.0.0.1") -> Page:
    from ssios.scope import AccountScope

    account = _account(session)
    scope = AccountScope(plane, account)
    if str(body.get("decision") or "") == "retry":
        return _retry_approval(plane, session, scope, approval_id, peer_ip)
    google_record = _google_fetch_record(plane, approval_id)
    if google_record is not None:
        return _decide_google(plane, session, scope, google_record, body, peer_ip)
    if scope.has_approval(approval_id):
        try:
            result = scope.decide(approval_id, body, peer_ip)
        except SsiosError as exc:
            if exc.code not in {"approval_replay_denied", "approval_expired"}:
                raise
            _log_approval(approval_id, "replay", scope.approvals().get(approval_id))
            return Page(200, _approval_status_page(plane, session, scope, approval_id, None))
        _log_approval(approval_id, "decide", scope.approvals().get(approval_id))
        return Page(200, _approval_status_page(plane, session, scope, approval_id, result))
    if account.get("legacy_owner"):
        result = plane.decide(_human(plane), approval_id, body)
        return Page(200, _page(session, "Decided", f"<p>{html.escape(str(result.get('status')))}</p>"))
    raise SsiosError("not_found", "approval not found", 404)


def _log_approval(approval_id: str, path: str, record: dict) -> None:
    import sys

    status = re.sub(r"[^a-z_]", "", str(record.get("status") or ""))[:24]
    action = re.sub(r"[^a-z.]", "", str(record.get("action") or ""))[:32]
    safe_id = re.sub(r"[^A-Za-z0-9-]", "", str(approval_id))[:40]
    sys.stderr.write(f"ssios.ui approval id={safe_id} path={path} status={status} action={action}\n")
    sys.stderr.flush()


def _approval_status_page(plane, session, scope, approval_id: str, result: dict | None, extra: str = "") -> str:
    from datetime import datetime

    from ssios.calendar_intent import account_zone

    record = scope.approvals().get(approval_id)
    status = str(record.get("status") or "")
    decision = str(record.get("decision") or "")
    if status == "consumed" and decision == "approve":
        label = "Approved"
    elif status == "denied":
        label = "Denied"
    elif status == "expired":
        label = "Expired"
    elif status == "superseded":
        label = "Superseded"
    else:
        label = status or "Unknown"
    zone, zone_name = account_zone(plane.settings, session.get("account"))
    stamp = record.get("decided_at") or record.get("expires_at")
    try:
        when = datetime.fromtimestamp(float(stamp), zone).replace(microsecond=0).isoformat()
    except (TypeError, ValueError, OSError):
        when = ""
    outcome = _approval_outcome(scope, record, result)
    found = scope.chat().find_approval(approval_id)
    conversation = found[0] if found else ""
    back = f"/ui/chat?conversation={html.escape(conversation)}" if conversation else "/ui/chat"
    retry = ""
    if status == "expired":
        csrf = html.escape(str(session.get("csrf") or ""))
        retry = (
            f"<form method=\"post\" action=\"/ui/approvals/{html.escape(approval_id)}\">"
            f"<input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
            "<button class=\"primary\" name=\"decision\" value=\"retry\" type=\"submit\">Retry</button></form>"
        )
    return _page(
        session,
        "Decided",
        "<h2>" + html.escape(label) + "</h2>"
        + (f"<p>When: <time datetime=\"{html.escape(when)}\">{html.escape(when)}</time> ({html.escape(zone_name)})</p>" if when else "")
        + f"<p>{html.escape(outcome)}</p>"
        + f"<p>Stored status: {html.escape(status)}</p>"
        + retry
        + extra
        + f"<p><a href=\"{back}\">Back to the conversation</a></p>",
    )


def _approval_outcome(scope, record: dict, result: dict | None) -> str:
    status = str(record.get("status") or "")
    if status == "denied":
        return "Nothing was changed."
    if status == "expired":
        return "This approval expired before it ran. Retry stages a new one."
    if status == "superseded":
        return "A newer approval replaced this one."
    action = str(record.get("action") or "")
    if action == "memory.commit":
        try:
            proposal = scope.memory().load_proposal(str(record.get("proposal_id") or ""))
            paths = ", ".join(str(item.get("path") or "") for item in proposal.get("files") or [])
        except SsiosError:
            paths = ""
        if paths:
            return "Saved to memory (" + paths + ")."
        return "Saved to memory."
    if action == "vault.clip":
        return "Saved the clip."
    if action == "chat.accept":
        return "Saved the guest result for this account."
    if action == "goal.update":
        return "Updated the task."
    if isinstance(result, dict) and result.get("status"):
        return "Status: " + str(result.get("status"))
    return "The decision was recorded."


def _retry_approval(plane, session, scope, approval_id: str, peer_ip: str) -> Page:
    del peer_ip
    if not scope.has_approval(approval_id):
        raise SsiosError("not_found", "approval not found", 404)
    store = scope.approvals()
    record = store.get(approval_id)
    if record.get("status") != "expired" or record.get("action") != "memory.commit":
        _log_approval(approval_id, "retry-rejected", record)
        return Page(200, _approval_status_page(plane, session, scope, approval_id, None))
    proposal = scope.memory().load_proposal(str(record.get("proposal_id") or ""))
    files = [item for item in proposal.get("files") or [] if isinstance(item, dict)]
    if not files:
        return Page(200, _page(session, "Decided", "<p>Nothing to stage again.</p>"))
    fresh = scope.memory().propose(scope.project_id(), scope.memory().head(scope.project_id()), files, task_id="chat")
    approval = scope.create_memory_approval(fresh)
    store = scope.approvals()
    record = store.get(approval_id)
    record["status"] = "superseded"
    store.save()
    _log_approval(approval_id, "retry", record)
    form = "<h2>New approval</h2>" + _decision_form(session, scope.approvals().get(approval["id"]))
    return Page(200, _approval_status_page(plane, session, scope, approval_id, None, form))


def _owns_google_fetch(plane, account_id: str, record: dict) -> bool:
    google = getattr(plane, "_google", None)
    if google is None:
        return False
    material = google._requests.get(record.get("approval_id"))
    return isinstance(material, dict) and material.get("account_id") == account_id


def _google_fetch_record(plane, approval_id: str) -> dict | None:
    try:
        record = plane.approvals.get(approval_id)
    except SsiosError:
        return None
    if record.get("action") not in ("connector.fetch", "connector.write"):
        return None
    return record


def _decide_google(plane, session: dict, scope, record: dict, body: dict, peer_ip: str) -> Page:
    from ssios.chat import continue_after_fetch
    from ssios.ui_household import _events_html

    account = session["account"]
    material = plane.google._requests.get(record["approval_id"])
    applied_owner = plane.google._applied.get(record["approval_id"])
    owner = material.get("account_id") if isinstance(material, dict) else applied_owner
    if owner != account["id"]:
        raise SsiosError("forbidden", "a session cannot act for another account", 403)
    try:
        result = plane.decide(_human(plane), record["approval_id"], body)
    except SsiosError as exc:
        from ssios.chat import english_connector_error

        return Page(exc.status, _page(session, "Decided", f"<p>{html.escape(english_connector_error(exc))}</p>"))
    if result.get("status") != "consumed":
        return Page(200, _page(session, "Decided", f"<p>{html.escape(str(result.get('status')))}</p>"))
    from ssios.google_connector import session_for_account

    taken = plane.google.take_result(session_for_account(account), record["approval_id"])
    tool = str(material.get("tool") or "")
    plane.security_audit.write(
        actor=account["id"],
        ip=peer_ip or "127.0.0.1",
        action=str(record.get("action") or "connector.fetch"),
        target=tool.split(".", 1)[0],
        result="ok",
        extra={"service": tool.split(".", 1)[0], "item_count": "1"},
    )
    found = scope.chat().find_approval(record["approval_id"])
    conversation_id = found[0] if found else ""
    message = found[1] if found else {}
    route_name = str(message.get("route_name") or "connector")
    followup = message.get("followup") if isinstance(message.get("followup"), dict) else None
    events = list(
        continue_after_fetch(
            plane,
            account,
            {
                "meta": {"conversation_id": conversation_id, "route": route_name, "followup": followup},
                "fetch": {"text": taken["text"]},
            },
            peer_ip,
        )
    )
    return Page(200, _page(session, "Decided", _events_html(session, events)))


def _ingest_form(plane, session: dict, notice: str) -> str:
    del plane
    csrf = html.escape(str(session["csrf"]))
    return _page(
        session,
        "Ingest",
        notice
        + f"<form method=\"post\" action=\"/ui/ingest\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        "<label>Project <input name=\"project_id\" value=\"local-summary\"></label>"
        "<label>Filename <input name=\"filename\" value=\"note.md\"></label>"
        "<label>Body <textarea name=\"content\" rows=\"8\"></textarea></label>"
        "<button type=\"submit\">Preview and create approval</button></form>",
    )


def _ingest(plane, session: dict, body: dict) -> Page:
    account = _account(session)
    if not account.get("legacy_owner"):
        raise SsiosError("forbidden", "ingest for another account is rejected", 403)
    project_id = str(body.get("project_id") or "")
    filename = str(body.get("filename") or "")
    content = str(body.get("content") or "")
    if "/" in filename or not filename.endswith((".md", ".txt")):
        raise SsiosError("ingest_rejected", "filename rejected", 400)
    _reject_secrets(content)
    if not content.startswith("---\n"):
        stem = filename.rsplit(".", 1)[0]
        content = f"---\nname: {stem}\ndescription: \"uploaded note\"\n---\n\n{content}\n"
    expected = plane.upstream.head(project_id) if plane._uses_upstream() else plane.memory.head(project_id)
    result = plane.propose_memory(
        _human(plane),
        {
            "project_id": project_id,
            "expected_revision": expected,
            "files": [{"path": f"topics/{project_id}/{filename}", "content": content}],
        },
    )
    preview = html.escape(content)
    return Page(
        200,
        _page(
            session,
            "Ingest submitted for approval",
            f"<pre>{preview}</pre><p>Approval {html.escape(str(result['approval']['id']))}. Memory has not been written.</p>",
        ),
    )


def _summary_post(plane, session: dict, body: dict) -> Page:
    account = _account(session)
    if not account.get("legacy_owner"):
        raise SsiosError("forbidden", "summary tasks stay on the original account", 403)
    created = plane.create_task(
        plane.auth.read_secret("human"),
        {
            "project_id": body.get("project_id", "local-summary"),
            "topic": body.get("topic") or body.get("project_id", "local-summary"),
            "kind": "project-summary",
            "input_revision": "exp-1",
            "input_path": "notes.md",
        },
    )
    return Page(200, _page(session, "Task created", f"<p>Task {html.escape(created['id'])} is queued. Approval still happens in the inbox.</p>"))


def _summary_form(plane, session: dict) -> str:
    del plane
    csrf = html.escape(str(session["csrf"]))
    return _page(
        session,
        "Summary",
        f"<form method=\"post\" action=\"/ui/summary\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        "<label>Project <input name=\"project_id\" value=\"local-summary\"></label>"
        "<label>Topic <input name=\"topic\" value=\"local-summary\"></label>"
        "<button type=\"submit\">Create summary task</button></form>",
    )


def _memory(plane, session: dict, project_id: str, path: str, query_text: str = "") -> str:
    account = _account(session)
    if not account.get("legacy_owner"):
        from ssios.scope import AccountScope

        scope = AccountScope(plane, account)
        project_id = scope.project_id()
        paths = scope.memory().list_markdown(project_id)
        content = ""
        if path:
            doc = scope.memory().read_file(project_id, path, include_untrusted=False)
            from ssios.vault import render_markdown

            _meta, rendered = render_markdown(str(doc.get("content") or ""), link_for=lambda url: url if str(url).startswith(("http://", "https://")) else "")
            trust = html.escape(str(doc.get("trust") or ""))
            revision = html.escape(str(doc.get("revision") or ""))
            content = (
                f"<p>Trust: {trust}. Revision: {revision}.</p>"
                + rendered
                + _account_history(scope, project_id, path)
            )
        shown = [item for item in paths if not query_text or query_text.casefold() in item.casefold()]
        links = "".join(
            f"<li><a href=\"/ui/memory?path={html.escape(item)}\">{html.escape(item)}</a></li>"
            for item in shown
        )
        safe_q = html.escape(query_text)
        search = (
            f"<form method=\"get\" action=\"/ui/memory\"><label>Search <input name=\"q\" value=\"{safe_q}\"></label>"
            "<button type=\"submit\">Search</button></form>"
        )
        from ssios.account_memory import write_mode

        mode = write_mode(scope.home)
        mode_form = (
            f"<form method=\"post\" action=\"/ui/memory\"><input type=\"hidden\" name=\"csrf\" value=\"{html.escape(str(session.get('csrf') or ''))}\">"
            f"<label>Memory writes <select name=\"mode\"><option {'selected' if mode == 'approve' else ''}>approve</option>"
            f"<option {'selected' if mode == 'auto' else ''}>auto</option></select></label>"
            "<button name=\"op\" value=\"mode\" type=\"submit\">Save</button></form>"
        )
        editor = ""
        if path and path in paths:
            raw = str(doc.get("content") or "")
            editor = (
                f"<form method=\"post\" action=\"/ui/memory\"><input type=\"hidden\" name=\"csrf\" value=\"{html.escape(str(session.get('csrf') or ''))}\">"
                f"<input type=\"hidden\" name=\"path\" value=\"{html.escape(path)}\">"
                f"<label>Edit <textarea name=\"content\" rows=\"10\">{html.escape(raw)}</textarea></label>"
                "<button name=\"op\" value=\"save\" type=\"submit\">Save</button> "
                "<button name=\"op\" value=\"forget\" type=\"submit\">Forget</button></form>"
            )
        return _page(session, "Memory", mode_form + search + f"<ul>{links}</ul>{content}{editor}")
    paths = plane.memory.list_markdown(project_id) if not plane._uses_upstream() else plane.upstream.list_markdown(project_id)
    links = "".join(
        f"<li><a href=\"/ui/memory?project_id={html.escape(project_id)}&path={html.escape(item)}\">{html.escape(item)}</a></li>"
        for item in paths
    )
    content = ""
    if path:
        doc = plane.read_memory(_human(plane), project_id, path)
        content = f"<pre>{html.escape(str(doc.get('content') or ''))}</pre>" + _history(plane, project_id, path)
    return _page(session, "Memory", f"<ul>{links}</ul>{content}")


def _memory_post(plane, session, body: dict, peer_ip: str) -> Page:
    from ssios.account_memory import set_write_mode
    from ssios.chat import apply_memory_change, restore_memory
    from ssios.memory_store import validate_document
    from ssios.scope import AccountScope

    account = _account(session)
    if account.get("legacy_owner"):
        raise SsiosError("forbidden", "use the account memory page", 403)
    scope = AccountScope(plane, account)
    op = str(body.get("op") or "")
    if op == "mode":
        set_write_mode(scope.home, str(body.get("mode") or "approve"))
        return Page(200, _memory(plane, session, scope.project_id(), "", ""))
    if op == "undo":
        restore_memory(scope, str(body.get("id") or ""), peer_ip)
        return Page(200, _memory(plane, session, scope.project_id(), "", ""))
    path = str(body.get("path") or "")
    known = scope.memory().list_markdown(scope.project_id())
    if path not in known:
        raise SsiosError("path_rejected", "memory path rejected", 400)
    if op == "forget":
        result = apply_memory_change(scope, [], peer_ip, forget_path=path)
    elif op == "save":
        content = str(body.get("content") or "")
        validate_document(path, content)
        result = apply_memory_change(scope, [{"path": path, "content": content}], peer_ip)
    else:
        raise SsiosError("memory_invalid", "memory action rejected", 400)
    if result["status"] == "pending":
        approval = scope.approvals().get(result["approval"]["id"])
        form = "<h2>Approve this memory change</h2>" + _decision_form(session, approval)
        return Page(200, _page(session, "Memory", form))
    return Page(200, _memory(plane, session, scope.project_id(), path, ""))


def _account_history(scope, project_id: str, path: str) -> str:
    repo = scope.memory().repo(project_id)
    completed = scope.memory().git.run(repo, ["log", "--pretty=format:%h %s", "-n", "8", "--", path], check=False)
    if completed.returncode != 0 or not completed.stdout:
        return ""
    text = completed.stdout.decode("utf-8", "replace")
    return f"<h2>History</h2><pre>{html.escape(text)}</pre>"


def _history(plane, project_id: str, path: str) -> str:
    if plane._uses_upstream():
        repo = plane.upstream._memory_dir(plane.upstream.ensure_agent(project_id))
        completed = plane.upstream._git(repo, ["log", "--pretty=format:%h %s", "-n", "8", "--", path], check=False)
    else:
        repo = plane.memory.repo(project_id)
        completed = plane.memory.git.run(repo, ["log", "--pretty=format:%h %s", "-n", "8", "--", path], check=False)
    if completed.returncode != 0 or not completed.stdout:
        return ""
    text = completed.stdout.decode("utf-8", "replace")
    return f"<h2>History</h2><pre>{html.escape(text)}</pre>"


def _vault_page(plane, session: dict, notice: str, tag: str = "") -> str:
    from ssios.errors import SsiosError
    from ssios.locale_messages import say
    from ssios.vault import list_notes

    try:
        notes = list_notes(plane.settings, session["account"]["id"])
    except SsiosError as exc:
        if exc.code != "path_rejected":
            raise
        notes = []
        notice = _clip_notice("clip_denied", say(ui_language(plane, session), "clip_denied"))
    from urllib.parse import quote

    from ssios.reading import depth_label, ledger

    seen = ledger(plane.accounts.home(session["account"]["id"]))
    wanted_tag = str(tag or "")
    folders: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    for item in notes:
        for name in item.get("tags") or []:
            counts[name] = counts.get(name, 0) + 1
        if wanted_tag and wanted_tag not in (item.get("tags") or []):
            continue
        href = "/ui/vault/note?path=" + quote(item["path"], safe="")
        depth = depth_label(seen.get("note:" + item["path"], {}).get("depth") or "skimmed")
        source = html.escape(item.get("source") or "")
        row = f'<li><a href="{html.escape(href, quote=True)}">{html.escape(item["title"])}</a> <span class="badge">{depth}</span> <span>{source}</span></li>'
        folders.setdefault(item.get("folder") or "", []).append(row)
    tree = []
    for folder in sorted(folders, key=lambda name: (name != "", name.casefold())):
        label = html.escape(folder or "Vault root")
        tree.append(f'<details class="vault-folder" open><summary>{label} <span class="count">{len(folders[folder])}</span></summary><ul>' + "".join(folders[folder]) + "</ul></details>")
    tags = "".join(
        f'<a{" class=\"active\"" if name == wanted_tag else ""} href="/ui/vault?tag={html.escape(quote(name, safe=""), quote=True)}">#{html.escape(name)} <span class="count">{count}</span></a> '
        for name, count in sorted(counts.items(), key=lambda row: (-row[1], row[0].casefold()))[:60]
    )
    csrf = html.escape(str(session.get("csrf") or ""))
    paste = (
        f"<form method=\"post\" action=\"/ui/vault\" enctype=\"multipart/form-data\">"
        f"<input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        "<label>Source URL <input name=\"url\" placeholder=\"https://example.com/story\"></label>"
        "<label>Pasted article <textarea name=\"content\" rows=\"6\"></textarea></label>"
        "<label>Or a text file <input type=\"file\" name=\"file\"></label>"
        "<button type=\"submit\">Clip pasted article</button></form>"
    )
    body = (
        notice
        + paste
        + "<p>These notes are on this computer. The folders are the folders of the vault, as Obsidian shows them. Open the vault folder in Obsidian.</p>"
        + (f'<p class="vault-tags">{tags}' + (' <a href="/ui/vault">All notes</a>' if wanted_tag else "") + "</p>" if tags else "")
        + ("".join(tree) or "<ul><li>No notes yet.</li></ul>")
    )
    return _page(session, "Notes", body)


def _vault_note(plane, session: dict, rel: str, notice: str = "") -> str:
    from urllib.parse import quote

    from ssios.reading import depth_label, ledger
    from ssios.vault import read_note, render_markdown, resolve_link

    account_id = session["account"]["id"]
    text = read_note(plane.settings, account_id, rel)

    def link_for(url: str) -> str:
        if url.startswith(("javascript:", "data:")):
            return ""
        if url.startswith(("https://", "http://")):
            return url
        found = resolve_link(plane.settings, account_id, rel, url)
        if not found:
            return ""
        kind = "note" if found.casefold().endswith(".md") else "asset"
        return f"/ui/vault/{kind}?path=" + quote(found, safe="")

    meta, rendered = render_markdown(text, link_for=link_for)
    props = "".join(f"<dt>{html.escape(key)}</dt><dd>{html.escape(value)}</dd>" for key, value in meta.items())
    entry = ledger(plane.accounts.home(account_id)).get("note:" + rel, {})
    depth = entry.get("depth") or "skimmed"
    csrf = html.escape(str(session.get("csrf") or ""))
    path = html.escape(rel, quote=True)
    crumbs = " / ".join(html.escape(part) for part in rel.split("/"))
    tools = (
        f'<section class="card reading"><p>{crumbs} <span class="badge">{depth_label(depth)}</span></p>'
        "<p>A clipped or written note counts as skimmed. Mark it read after reading it with care. Chat says which one it is when it uses the note.</p>"
        f'<form method="post" action="/ui/vault/note"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="path" value="{path}">'
        + ('<button name="op" value="read" type="submit">Mark as read</button> ' if depth != "read" else "")
        + '<label>Remind me to read this <select name="when"><option value="tonight">tonight</option><option value="tomorrow" selected>tomorrow morning</option>'
        '<option value="weekend">this weekend</option><option value="week">in a week</option></select></label> '
        '<button name="op" value="remind" type="submit">Set reminder</button></form></section>'
    )
    return _page(session, "Note", f"{notice}{tools}<dl>{props}</dl>{rendered}<p><a href=\"/ui/vault\">All notes</a></p>")


def _vault_note_post(plane, session: dict, body: dict, peer_ip: str) -> Page:
    from ssios.reading import mark_note, remind_to_read
    from ssios.scope import AccountScope
    from ssios.vault import read_note

    rel = str(body.get("path") or "")
    read_note(plane.settings, session["account"]["id"], rel)
    scope = AccountScope(plane, _account(session))
    if str(body.get("op") or "") == "remind":
        when = remind_to_read(plane, scope, "note:" + rel, rel.rsplit("/", 1)[-1][:-3], str(body.get("when") or "tomorrow"))
        notice = f"<p>Reminder set for {html.escape(when)}.</p>"
    else:
        mark_note(plane, scope, rel, "read", "marked", peer_ip)
        notice = "<p>Marked as read.</p>"
    return Page(200, _vault_note(plane, session, rel, notice))


def _clip_notice(code: str, detail: str) -> str:
    safe_code = html.escape(code, quote=True)
    return f'<p class="notice" data-code="{safe_code}">{html.escape(detail)}</p>'


def _pasted_text(body: dict) -> str:
    content = str(body.get("content") or "")
    if content.strip():
        return content
    uploads = body.get("uploads") if isinstance(body.get("uploads"), list) else []
    if not uploads or not isinstance(uploads[0], dict):
        return content
    raw = uploads[0].get("bytes")
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        return content
    try:
        return bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _vault_paste(plane, session: dict, body: dict, peer_ip: str) -> Page:
    from ssios.clipper import clip_from_browser
    from ssios.errors import SsiosError
    from ssios.scope import AccountScope

    url = str(body.get("url") or "")
    content = _pasted_text(body)
    try:
        result = clip_from_browser(plane, session["account"]["id"], {"url": url, "html": content, "title": "Pasted article"}, peer_ip)
    except SsiosError as exc:
        return Page(exc.status, _vault_page(plane, session, _clip_notice(exc.code, exc.detail)))
    if result.get("status") == "saved":
        return Page(200, _vault_page(plane, session, f"<p>Saved the clip to {html.escape(str(result.get('path') or ''))}.</p>"))
    scope = AccountScope(plane, session["account"])
    record = scope.approvals().get(str(result.get("approval_id") or ""))
    note = "This page looks truncated or paywalled. " if result.get("truncated") else ""
    return Page(200, _page(session, "Notes", f"<p>{note}Waiting for your approval. Nothing is written until you approve it.</p>" + _decision_form(session, record)))


def _vault_asset(plane, session: dict, rel: str) -> Page:
    from ssios.vault import read_asset

    data, content_type = read_asset(plane.settings, session["account"]["id"], rel)
    return Page(200, data, content_type=content_type)


_CLIP_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "content-type, x-ssios-clip-token",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
}


def _browser_clip(plane, method: str, headers, body: dict, peer_ip: str) -> Page:
    from ssios.clipper import account_for_token, clip_from_browser

    if method == "OPTIONS":
        return Page(204, "", content_type="text/plain; charset=utf-8", extra_headers=_CLIP_HEADERS)
    if method != "POST":
        raise SsiosError("method_not_allowed", "method rejected", 405)
    token = ""
    if headers is not None:
        token = str(headers.get("X-SSIOS-Clip-Token") or "")
    account_id = account_for_token(plane, token)
    if str(body.get("op") or "") == "save-session":
        from ssios.clipper import save_site_from_browser

        result = save_site_from_browser(plane, account_id, body, peer_ip)
    else:
        result = clip_from_browser(plane, account_id, body, peer_ip)
    return Page(200, json.dumps(result), content_type="application/json; charset=utf-8", extra_headers=_CLIP_HEADERS)


def _clipper_page(plane, session: dict, notice: str, token: str = "") -> str:
    from ssios.clipper import token_status
    from ssios.site_session import list_sessions

    account_id = session["account"]["id"]
    csrf = html.escape(str(session.get("csrf") or ""))
    status = token_status(plane, account_id)
    shown = f"<p><code>{html.escape(token)}</code></p><p>Copy this token now. It is not shown again.</p>" if token else ""
    rows = []
    for item in list_sessions(plane, account_id):
        rows.append(
            f"<li>{html.escape(item['domain'])} {html.escape(item.get('label') or '')} "
            f"<form method=\"post\" action=\"/ui/clipper\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
            f"<input type=\"hidden\" name=\"domain\" value=\"{html.escape(item['domain'])}\">"
            "<button name=\"op\" value=\"delete-site\" type=\"submit\">Delete</button></form></li>"
        )
    body = (
        notice
        + "<h2>Clip token</h2>"
        + f"<p>Status: {html.escape(status)}. The browser sends this token in a header. The session cookie is not used.</p>"
        + shown
        + f"<form method=\"post\" action=\"/ui/clipper\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        "<button name=\"op\" value=\"issue\" type=\"submit\">Generate token</button> "
        "<button name=\"op\" value=\"revoke\" type=\"submit\">Revoke</button></form>"
        + "<p><a href=\"/ui/clipper\">Install notes are in docs/clipper.md</a></p>"
        + "<h2>Site sessions</h2>"
        + "<p>Open the site while you are logged in, then click Save site session in the extension or bookmarklet. Status is on Connections. The cookie stays in the OS secret store. SSIOS does not store a password and does not log in for you.</p>"
        + "<details><summary>Paste a cookie instead</summary>"
        + f"<form method=\"post\" action=\"/ui/clipper\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        "<label>Domain <input name=\"domain\" placeholder=\"example.com\"></label>"
        "<label>Cookie header <textarea name=\"cookie\" rows=\"3\"></textarea></label>"
        "<button name=\"op\" value=\"save-site\" type=\"submit\">Save pasted cookie</button></form></details>"
        + "<ul>" + ("".join(rows) or "<li>No site sessions.</li>") + "</ul>"
        + f"<form method=\"post\" action=\"/ui/clipper\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        "<button name=\"op\" value=\"undo-clip\" type=\"submit\">Undo last auto clip</button></form>"
    )
    return _page(session, "Clipper", body)


def _clipper_post(plane, session: dict, body: dict) -> Page:
    from ssios.clipper import issue_token, revoke_token, undo_last_clip
    from ssios.site_session import delete_session, save_session

    account_id = session["account"]["id"]
    op = str(body.get("op") or "")
    if op == "issue":
        token = issue_token(plane, account_id)
        return Page(200, _clipper_page(plane, session, "", token))
    if op == "revoke":
        revoke_token(plane, account_id)
        return Page(200, _clipper_page(plane, session, "<p>Token revoked.</p>"))
    if op == "save-site":
        save_session(plane, account_id, str(body.get("domain") or ""), str(body.get("cookie") or ""))
        return Page(200, _clipper_page(plane, session, "<p>Site session saved. The cookie was not written to disk.</p>"))
    if op == "delete-site":
        delete_session(plane, account_id, str(body.get("domain") or ""))
        return Page(200, _clipper_page(plane, session, "<p>Site session deleted.</p>"))
    if op == "undo-clip":
        undo_last_clip(plane, account_id)
        return Page(200, _clipper_page(plane, session, "<p>The last auto clip was removed.</p>"))
    from ssios.locale_messages import say

    raise SsiosError("clip_denied", say(ui_language(plane, session), "clip_denied"), 400)


def _settings(plane, session: dict) -> str:
    status = plane.status(_human(plane))
    safe = {
        "backend": status.get("runtime", {}).get("active_backend"),
        "isolated": status.get("runtime", {}).get("isolated"),
        "memory": status.get("memory", {}).get("mode"),
        "model": status.get("llm", {}).get("model"),
    }
    policy = plane.skills.policy()
    prefs = plane.skills.account_prefs(session["account"]["id"])
    csrf = html.escape(str(session["csrf"]))
    location = html.escape(str(prefs.get("location") or ""))
    switches = []
    for name in ("weather", "web_fetch"):
        on = name in prefs.get("enabled", [])
        verb = "disable" if on else "enable"
        label = "On" if on else "Off"
        switches.append(
            f"<form method=\"post\" action=\"/ui/settings\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
            f"<input type=\"hidden\" name=\"skill\" value=\"{name}\">"
            f"<span>{name}: {label}</span> <button name=\"op\" value=\"{verb}\" type=\"submit\">{verb.title()}</button></form>"
        )
    ceiling = "Web skills are on." if policy["enabled"] else "Web skills are off until an admin turns them on."
    form = (
        f"<section class=\"card\"><h2>Web skills</h2><p>{ceiling}</p>"
        + "".join(switches)
        + f"<form method=\"post\" action=\"/ui/settings\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        f"<label>Default city <input name=\"location\" value=\"{location}\" maxlength=\"80\"></label> "
        "<button type=\"submit\">Save city</button></form>"
        f"<form method=\"post\" action=\"/ui/settings\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        f"<label>Language <select name=\"language\"><option value=\"en\" {'selected' if prefs.get('language') != 'zh' else ''}>English</option>"
        f"<option value=\"zh\" {'selected' if prefs.get('language') == 'zh' else ''}>中文</option></select></label> "
        f"<label>Units <select name=\"units\"><option value=\"celsius\" {'selected' if prefs.get('units') != 'fahrenheit' else ''}>Celsius</option>"
        f"<option value=\"fahrenheit\" {'selected' if prefs.get('units') == 'fahrenheit' else ''}>Fahrenheit</option></select></label> "
        "<button type=\"submit\">Save language and units</button></form>"
        f"<form method=\"post\" action=\"/ui/settings\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        f"<input type=\"hidden\" name=\"auto_save\" value=\"{'off' if prefs.get('auto_save') else 'on'}\">"
        f"<button type=\"submit\">{'Turn auto-save off' if prefs.get('auto_save') else 'Turn auto-save on'}</button></form>"
        "<p>Auto-save is off by default. A clip is written to your vault only after you approve it.</p>"
        "<p><a href=\"/ui/clipper\">Browser clipper</a></p>"
        f"<form method=\"post\" action=\"/ui/settings\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        f"<label>Time zone <input name=\"time_zone\" value=\"{html.escape(str((plane.accounts.users.get(session['account']['id']) or {}).get('time_zone') or ''))}\" placeholder=\"America/Los_Angeles\"></label> "
        "<button type=\"submit\">Save time zone</button></form>"
        "</section>"
        + _brief_form(plane, session, csrf)
    )
    return _page(session, "Settings", form + f"<pre>{html.escape(json.dumps(safe, ensure_ascii=False, indent=2))}</pre>")


def _brief_form(plane, session: dict, csrf: str) -> str:
    from ssios.background import brief_settings

    brief = brief_settings(plane.accounts.home(session["account"]["id"]))
    checked = " checked" if brief["enabled"] else ""
    return (
        "<section class=\"card\"><h2>Daily brief</h2>"
        "<p>Once a day, a new chat lists today's reminders, tasks that are due, and calendar events. "
        "It is built from stored data and a calendar read. No model writes it. Off by default.</p>"
        f"<form method=\"post\" action=\"/ui/settings\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        "<input type=\"hidden\" name=\"brief\" value=\"1\">"
        f"<label><input type=\"checkbox\" name=\"brief_enabled\" value=\"1\"{checked}> Post a daily brief</label> "
        f"<label>At <input name=\"brief_time\" value=\"{html.escape(brief['time'], quote=True)}\" maxlength=\"5\" placeholder=\"08:00\"></label> "
        "<button type=\"submit\">Save daily brief</button></form></section>"
    )


def _settings_post(plane, session: dict, body: dict) -> Page:
    account_id = session["account"]["id"]
    if body.get("brief"):
        from ssios.background import set_brief

        try:
            set_brief(plane.accounts.home(account_id), enabled=bool(body.get("brief_enabled")), at=str(body.get("brief_time") or ""))
        except SsiosError as exc:
            return Page(exc.status, _page(session, "Settings", f"<p>{html.escape(exc.detail)}</p>"))
        return Page(200, _settings(plane, session))
    location = body.get("location")
    skill = str(body.get("skill") or "")
    op = str(body.get("op") or "")
    if skill and op in {"enable", "disable"}:
        plane.skills.set_account(account_id, name=skill, enabled=op == "enable")
    elif body.get("auto_save") in {"on", "off"}:
        plane.skills.set_account(account_id, auto_save=body.get("auto_save") == "on")
    elif body.get("link_style") in {"relative", "wikilink"}:
        plane.skills.set_account(account_id, link_style=str(body.get("link_style")))
    elif "time_zone" in body:
        try:
            plane.accounts.set_time_zone(account_id, str(body.get("time_zone") or ""))
        except SsiosError as exc:
            return Page(exc.status, _page(session, "Settings", f"<p>{html.escape(exc.detail)}</p>"))
    elif body.get("language") in {"en", "zh"} or body.get("units") in {"celsius", "fahrenheit"}:
        language = body.get("language") if body.get("language") in {"en", "zh"} else None
        units = body.get("units") if body.get("units") in {"celsius", "fahrenheit"} else None
        plane.skills.set_account(account_id, language=language, units=units)
        from ssios.account_memory import commit_preference_facts
        from ssios.scope import AccountScope

        facts = []
        if language:
            facts.append({"path": "preferences.md", "key": "Language", "value": language})
        if units:
            facts.append({"path": "preferences.md", "key": "Units", "value": units})
        if facts:
            try:
                commit_preference_facts(AccountScope(plane, session["account"]), facts, "127.0.0.1")
            except SsiosError:
                return Page(500, _page(session, "Settings", "<p>The setting was not saved.</p>"))
    elif location is not None:
        plane.skills.set_account(account_id, location=str(location))
    return Page(200, _settings(plane, session))


APP_CSS = """
:root {
  color-scheme: light dark;
  --bg: #fafafa; --ink: #171717; --muted: #525252; --panel: #ffffff; --line: #e7e7e7;
  --user: #dbe7ff; --accent: #1d4ed8; --on-accent: #ffffff; --hover: #f3f4f6; --ok-dot: #15803d;
  --danger: #b91c1c; --danger-bg: #fef2f2; --ok: #166534; --warn: #92400e; --warn-bg: #fef3c7; --ok-bg: #dcfce7;
  --code: #f4f4f5;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f0f10; --ink: #f5f5f5; --muted: #a3a3a3; --panel: #171717; --line: #2a2a2a;
    --user: #1e3a5f; --accent: #93c5fd; --on-accent: #0f0f10; --hover: #222226; --ok-dot: #4ade80;
    --danger: #fecaca; --danger-bg: #3f1515; --ok: #bbf7d0; --warn: #fde68a; --warn-bg: #3f2e12; --ok-bg: #14301f;
    --code: #1c1c1f;
  }
}
*, *::before, *::after { box-sizing: border-box; }
html, body { height: 100%; }
body { margin: 0; overflow: auto; font: 15px/1.6 -apple-system, "SF Pro", "PingFang SC", "Noto Sans SC", sans-serif; color: var(--ink); background: var(--bg); }
body.app-body { overflow: hidden; }
h1, h2, h3, p, ul, ol, figure { margin: 0; }
ul, ol { padding: 0; list-style: none; }
a { color: inherit; text-decoration: none; }
summary { list-style: none; cursor: pointer; }
summary::-webkit-details-marker { display: none; }
button, input, textarea, select { font: inherit; color: inherit; }
.page a, main a, .msg a { color: var(--accent); }
.page a.button, .page a.button:hover { color: var(--on-accent); text-decoration: none; }
.msg ul { list-style: disc; padding-left: 1.25rem; margin: 8px 0; }
h1 { font-weight: 600; font-size: 1.35rem; margin: 0 0 16px; }
main { max-width: 760px; margin: 0 auto; padding: 24px 16px 48px; }
button, input, textarea, select { font: inherit; color: var(--ink); background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 8px 12px; }
button { background: transparent; cursor: pointer; width: auto; }
button.primary, a.button.primary { display: inline-block; background: var(--accent); color: var(--on-accent); border-color: transparent; text-decoration: none; }
button:disabled { opacity: 0.45; cursor: not-allowed; }
button.danger, .card.danger button.primary { background: var(--danger); color: #fff; border-color: transparent; }
@media (prefers-color-scheme: dark) {
  button.danger, .card.danger button.primary { color: #3f1515; }
}
label { display: grid; gap: 4px; }
form { display: grid; gap: 12px; }
.goal-actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.goal-actions button { width: auto; }
.checklist, .msg ul.checklist { list-style: none; margin: 8px 0; padding: 0; display: grid; gap: 8px; }
.checklist li.task { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.checklist form, .checklist .row { display: flex; align-items: flex-start; gap: 10px; margin: 0; }
.checklist .check {
  width: 18px; height: 18px; min-width: 18px; padding: 0; border-radius: 3px;
  border: 1.5px solid var(--ink); background: var(--panel); display: inline-grid; place-items: center;
}
.checklist .check[aria-pressed="true"] { background: var(--ink); color: var(--bg); }
.checklist .check[aria-pressed="true"]::after {
  content: ""; width: 8px; height: 4px; border-left: 2px solid currentColor; border-bottom: 2px solid currentColor;
  transform: rotate(-45deg) translateY(-1px);
}
.checklist .task.done .label { color: var(--muted); text-decoration: line-through; }
.checklist .check[aria-disabled="true"] { cursor: default; }
.md li.task { list-style: none; margin-left: 0; }
.md li.task input[type="checkbox"] {
  appearance: none; width: 16px; height: 16px; margin: 0 8px 0 0; vertical-align: -3px;
  border: 1.5px solid var(--ink); border-radius: 3px; background: var(--panel);
}
.md li.task.done { color: var(--muted); text-decoration: line-through; }
.md li.task.done input[type="checkbox"] { background: var(--ink); }
.badge { display: inline-block; min-width: 1.2em; margin-left: 6px; padding: 0 6px; border-radius: 999px; background: #b45309; color: #fff; font-size: 12px; text-align: center; }
pre, code { font-family: ui-monospace, "SF Mono", "Sarasa Mono SC", monospace; }
.app { height: 100%; display: flex; min-height: 0; min-width: 0; max-width: 100%; }
.rail { width: 64px; flex: none; display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 12px 6px; border-right: 1px solid var(--line); background: var(--bg); }
.rail a, .rail button { width: 44px; min-height: 36px; display: grid; place-items: center; border-radius: 12px; text-decoration: none; color: var(--ink); font-size: 12px; padding: 4px; }
.rail a:hover, .rail a:focus-visible, .rail button:hover, .rail button:focus-visible { background: var(--panel); }
.rail form { margin: 0; }
.rail-gap { flex: 1; }
.sr { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); border: 0; }
.finder { display: grid; gap: 4px; font-size: 12px; color: var(--muted); margin-bottom: 8px; }
.pin { display: block; padding: 8px 12px; border-radius: 12px; text-decoration: none; color: var(--ink); font-weight: 600; }
.side-head { display: flex; align-items: center; justify-content: space-between; }
.plus { text-decoration: none; color: var(--ink); border: 1px solid var(--line); border-radius: 999px; width: 28px; height: 28px; display: grid; place-items: center; }
.muted { color: var(--muted); font-size: 13px; }
.threads { list-style: none; margin: 0; padding: 0; }
.workspace { flex: 1; min-height: 0; display: flex; }
.thread-head { display: flex; align-items: center; justify-content: space-between; padding: 12px 16px 0; }
.thread-head h1 { margin: 0; font-size: 1rem; }
.info { width: 320px; flex: none; border-left: 1px solid var(--line); overflow: auto; padding: 16px; background: var(--bg); }
.who { display: flex; gap: 12px; align-items: center; margin-bottom: 8px; }
.who .avatar { width: 44px; height: 44px; font-size: 18px; }
.presence { display: flex; align-items: center; gap: 6px; color: var(--ok); font-size: 13px; margin: 0; }
.model-chip { display: inline-block; margin: 0 0 12px; font-size: 12px; color: var(--muted); background: var(--hover); border-radius: 999px; padding: 2px 8px; }
.tabs { display: flex; gap: 2px; margin-bottom: 16px; padding: 3px; background: var(--hover); border-radius: 12px; }
.tabs button { flex: 1; border: 0; border-radius: 9px; background: transparent; font-size: 12px; display: inline-flex; gap: 4px; align-items: center; justify-content: center; padding: 6px 4px; }
.tabs button[aria-selected="true"] { background: var(--panel); color: var(--ink); box-shadow: 0 1px 2px rgba(0,0,0,.06); }
.routines, .activity { list-style: none; margin: 0; padding: 0; display: grid; gap: 4px; }
.routines li { display: grid; grid-template-columns: 20px 1fr; gap: 8px; align-items: start; background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 10px; }
.routines li p, .activity li p { color: var(--muted); font-size: 13px; margin: 0; }
.activity li { display: grid; grid-template-columns: 20px 1fr auto; gap: 8px; align-items: start; background: transparent; border: 0; padding: 8px 0; }
.activity time { color: var(--muted); font-size: 12px; white-space: nowrap; }
.day-group { margin-top: 8px; }
.day-group summary { font-size: 11px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }
.quote, .quote-bar { border-left: 3px solid var(--accent); margin: 0 0 6px; padding: 2px 8px; color: var(--muted); font-size: 13px; }
.rail .new-chat { display: grid; margin: 0; width: 44px; border: 0; padding: 4px; }
.thumb { display: block; max-width: 180px; border-radius: 12px; margin-top: 6px; cursor: pointer; }
.paste-preview { display: flex; flex-wrap: wrap; gap: 8px; }
.paste-thumb { position: relative; margin: 0; }
.paste-thumb img { width: 72px; height: 72px; object-fit: cover; border-radius: 8px; display: block; }
.paste-x { position: absolute; top: 2px; right: 2px; width: 20px; height: 20px; border: 0; border-radius: 999px; background: var(--ink); color: var(--bg); line-height: 20px; padding: 0; cursor: pointer; }
.lightbox { position: fixed; inset: 0; background: rgba(0, 0, 0, .72); display: flex; align-items: center; justify-content: center; z-index: 30; }
.lightbox[hidden] { display: none; }
.lightbox img { max-width: 90vw; max-height: 90vh; }
.file-viewer .viewer-frame { background: var(--panel); border-radius: 12px; width: min(1000px, 94vw); height: 92vh; display: flex; flex-direction: column; overflow: hidden; }
.file-viewer .viewer-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 8px 12px; border-bottom: 1px solid var(--line); }
.file-viewer .viewer-head strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.file-viewer .viewer-links { display: flex; gap: 12px; align-items: center; flex: none; }
.file-viewer .viewer-body { flex: 1; min-height: 0; display: flex; align-items: center; justify-content: center; background: var(--bg); }
.file-viewer iframe { width: 100%; height: 100%; border: 0; }
.file-viewer img { max-width: 100%; max-height: 100%; }
.open-here { border: 1px solid var(--line); background: var(--panel); color: var(--accent); border-radius: 8px; padding: 4px 10px; cursor: pointer; font: inherit; }
.day { align-self: center; color: var(--muted); font-size: 12px; }
.attach input { width: 1px; height: 1px; opacity: 0; position: absolute; }
.attach { position: relative; width: 36px; height: 36px; display: grid; place-items: center; border: 1px solid var(--line); border-radius: 999px; cursor: pointer; }
.sidebar { width: 260px; flex: none; display: flex; flex-direction: column; border-right: 1px solid var(--line); padding: 16px 12px; background: var(--bg); }
.brand { font-weight: 650; color: var(--ink); text-decoration: none; }
.new-chat { display: block; margin: 16px 0; text-align: center; text-decoration: none; border: 1px solid var(--line); border-radius: 12px; padding: 8px 12px; color: var(--ink); }
.rail .new-chat { display: grid; margin: 0; width: 44px; min-height: 36px; border: 0; padding: 4px; text-align: center; }
.convs { flex: 1; overflow: auto; min-height: 0; }
.sidebar h2, .convs h2 { font-size: 11px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); margin: 16px 8px 4px; }
.sidebar .pin, .sidebar .conv a { border-radius: 10px; }
.sidebar .pin:hover, .sidebar .conv:hover, .sidebar .pin:focus-within, .sidebar .conv:focus-within { background: var(--hover); }
.sidebar .pin.active, .sidebar .conv.active { background: var(--user); }
.convs ul { margin: 0; padding: 0; list-style: none; }
.conv { display: flex; align-items: center; gap: 4px; border-radius: 12px; }
.conv a { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ink); text-decoration: none; padding: 8px 12px; }
.conv:hover, .conv:focus-within { background: var(--panel); }
.kebab { position: relative; }
.kebab summary { list-style: none; cursor: pointer; opacity: 0; border-radius: 8px; padding: 4px 8px; }
.conv:hover .kebab summary, .kebab[open] summary, .kebab summary:focus { opacity: 1; }
.conv .kebab form { position: absolute; right: 0; z-index: 2; background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 8px; width: 200px; }
tr:hover .kebab summary, .page .kebab summary { opacity: 1; }
.dock { margin-top: auto; position: relative; }
.account-row { display: flex; align-items: center; gap: 8px; padding: 8px; border-radius: 12px; }
.account-row:hover { background: var(--panel); }
.avatar, .mark { width: 28px; height: 28px; border-radius: 999px; display: inline-grid; place-items: center; background: var(--user); font-size: 13px; flex: none; }
.popover { position: absolute; left: 0; right: 0; bottom: calc(100% + 8px); z-index: 5; display: grid; gap: 2px; background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 8px; box-shadow: 0 8px 24px rgba(0,0,0,.08); }
.popover a, .popover button { display: block; width: 100%; box-sizing: border-box; margin: 0; padding: 8px 12px; border: 0; border-radius: 8px; background: transparent; color: var(--ink); text-align: left; text-decoration: none; }
.popover a:hover, .popover button:hover, .popover a:focus-visible, .popover button:focus-visible { background: var(--bg); }
.popover form { display: block; }
.menu-label { margin: 8px 12px 4px; padding-top: 8px; border-top: 1px solid var(--line); color: var(--muted); font-size: 12px; }
.stage { flex: 1; min-width: 0; min-height: 0; display: flex; flex-direction: column; overflow: hidden; }
.page { flex: 1; min-height: 0; overflow-y: auto; width: 100%; max-width: none; margin: 0; padding: 0; }
.sheet { max-width: 760px; margin: 0 auto; padding: 24px 16px 48px; }
.sheet.wide { max-width: 1100px; }
.page-bleed { max-width: 100%; min-width: 0; height: 100%; overflow: auto; padding: 12px 16px 28px; }
.page-bleed > h1 { margin: 0 0 12px; font-size: 1.35rem; }
.menu-btn { display: none; margin: 8px 16px 0; width: max-content; }
.thread { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.log { flex: 1; overflow: auto; min-height: 0; }
.column { max-width: 720px; margin: 0 auto; padding: 24px 16px 8px; display: flex; flex-direction: column; gap: 16px; }
.msg { display: flex; gap: 8px; align-items: flex-end; }
.msg.user { justify-content: flex-end; }
.msg.user .bubble { max-width: min(75%, 520px); background: var(--user); border-radius: 18px 18px 6px 18px; padding: 10px 14px; }
.msg.assistant .body { min-width: 0; flex: 1; max-width: min(100%, 560px); background: var(--panel); border: 1px solid var(--line); border-radius: 18px 18px 18px 6px; padding: 10px 14px; }
.msg .body { min-width: 0; flex: 1; }
.tools { display: flex; gap: 8px; align-items: center; }
.copy, .stamp { opacity: 0; font-size: 12px; color: var(--muted); background: transparent; border: 0; padding: 0; }
.msg:hover .copy, .msg:focus-within .copy, .msg:hover .stamp, .msg:focus-within .stamp, .codeblock:hover .copy, .codeblock:focus-within .copy { opacity: 1; }
.pill { align-self: flex-start; width: auto; font-size: 12px; color: var(--muted); border: 1px solid var(--line); border-radius: 999px; background: transparent; padding: 2px 8px; cursor: help; }
.codeblock { background: var(--code); border-radius: 12px; margin: 8px 0; }
.codehead { display: flex; justify-content: space-between; padding: 4px 8px; color: var(--muted); font-size: 12px; }
.codeblock pre { margin: 0; padding: 8px 12px 12px; white-space: pre-wrap; }
.empty { text-align: center; margin: auto; padding: 48px 16px; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; }
.chip { border-radius: 999px; }
.composer { padding: 8px 16px 20px; }
.composer-box { max-width: 720px; margin: 0 auto; border: 1px solid var(--line); border-radius: 28px; background: var(--panel); padding: 8px 8px 8px 16px; box-shadow: 0 10px 30px rgba(0,0,0,.08); display: grid; gap: 4px; }
.composer-box textarea { border: 0; resize: none; min-height: 24px; max-height: 160px; padding: 8px 0; background: transparent; width: 100%; outline: none; }
.composer-bar { display: flex; align-items: center; gap: 8px; }
.composer-bar .send { margin-left: auto; }
.route-chip select { border: 0; background: var(--hover); border-radius: 999px; padding: 4px 10px; font-size: 12px; }
.send, #mic, .attach { width: 36px; height: 36px; border-radius: 999px; display: grid; place-items: center; padding: 0; border: 0; background: transparent; }
.send:disabled { opacity: .4; }
.hint { max-width: 720px; margin: 8px auto 0; color: var(--muted); font-size: 12px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 16px; padding: 16px; }
.card.danger { border-color: var(--danger); background: var(--danger-bg); }
.card header, .card .actions { display: flex; align-items: center; gap: 8px; }
.card .actions { justify-content: flex-end; }
.card dl { display: grid; grid-template-columns: 8rem 1fr; gap: 4px 12px; margin: 12px 0; }
.card dt { color: var(--muted); }
.status { font-size: 12px; border-radius: 999px; padding: 2px 8px; background: var(--warn-bg); color: var(--warn); }
.status.approved { background: var(--ok-bg); color: var(--ok); }
.status.denied, .status.expired, .status.missing { background: var(--danger-bg); color: var(--danger); }
.diff s { color: var(--muted); }
.diff .add { background: var(--ok-bg); border-radius: 8px; padding: 2px 6px; }
.steps { list-style: none; padding: 0; margin: 0; display: flex; flex-wrap: nowrap; gap: 12px; color: var(--muted); font-size: 13px; align-items: center; white-space: nowrap; }
.steps li { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.step-details { display: inline-flex; margin: 0; color: var(--muted); font-size: 12px; }
.step-details summary { cursor: pointer; display: inline; }
.step-details:not([open]) > :not(summary) { display: none; }
.steps .done::before { content: "\\2713  "; color: var(--ok); }
.steps .spin::before { content: "\\25CC  "; }
.steps .fail::before { content: "\\2717  "; color: var(--danger); }
.choices { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
.choices .choice { border: 1px solid var(--line); background: var(--panel); border-radius: 999px; padding: 4px 10px; cursor: pointer; }
.codex-steps { list-style: none; padding: 0; margin: 8px 0; display: flex; flex-direction: column; gap: 6px; }
.codex-step { margin: 0; color: var(--muted); font-size: 13px; }
.badge.read, .badge.write { display: inline-block; margin-right: 6px; padding: 1px 6px; border-radius: 999px; font-size: 11px; letter-spacing: 0.04em; text-transform: uppercase; }
.badge.read { background: var(--user); color: var(--ink); }
.badge.write { background: var(--danger-bg); color: var(--danger); }
.error-card { border: 1px solid var(--line); border-left: 3px solid var(--danger); border-radius: 12px; padding: 12px 16px; background: var(--panel); }
.error-card .code { font-family: ui-monospace, monospace; font-size: 13px; }
.notice { margin: 0 0 12px; padding: 8px 12px; border-radius: 12px; background: var(--warn-bg); color: var(--warn); }
.notice.ok { background: var(--ok-bg); color: var(--ok); }
.setup-steps { display: flex; flex-wrap: wrap; gap: 12px; margin: 0 0 16px; color: var(--muted); }
.setup-steps .done { color: var(--ink); font-weight: 600; }
.help { list-style: decimal; padding-left: 1.25rem; }
.result-block { display: grid; gap: 8px; margin: 4px 0 12px; padding-top: 8px; }
.result-block + .result-block { border-top: 1px solid var(--line); }
.result-block h2, .result-block h3 { margin: 0; font-size: 0.95rem; }
.event-card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 12px; }
.event-card h3, .event-card h4 { margin: 0; font-size: 1rem; }
.event-card p { margin: 4px 0 0; }
.file-filters { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
.file-filters a { text-decoration: none; }
.file-search { display: flex; gap: 8px; align-items: end; margin-bottom: 16px; }
.file-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }
.library { position: relative; display: grid; grid-template-columns: minmax(0, 220px) minmax(0, 1fr); gap: 16px; align-items: start; min-width: 0; max-width: 100%; }
.library.has-selection { grid-template-columns: minmax(0, 220px) minmax(0, 1fr) minmax(180px, 280px); }
.drop-overlay { position: fixed; inset: 16px; display: none; align-items: center; justify-content: center; border: 3px dashed var(--accent); background: color-mix(in srgb, var(--accent) 14%, transparent); color: var(--ink); font-size: 1.5rem; font-weight: 600; z-index: 30; pointer-events: none; }
.drop-overlay.show { display: flex; }
.lib-folder.drop-hot { outline: 2px solid var(--accent); border-radius: 8px; }
.drop-log { list-style: none; margin: 8px 0; padding: 0; display: grid; gap: 4px; }
.drop-log li { font-size: 13px; }
.drop-log li.ask { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding: 8px 10px; border: 1px solid var(--line); border-radius: 12px; background: var(--panel); }
.drop-log li.ask label { display: flex; flex-direction: row; gap: 6px; align-items: center; }
.drop-log .bad { color: var(--danger); }
.drop-log button, .conflict-choice button { width: auto; }
.conflict-choice { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.lib-side, .lib-main, .lib-inspector, .lib-toolbar, .lib-grid { display: grid; gap: 10px; align-content: start; min-width: 0; max-width: 100%; }
.lib-main, .lib-toolbar { display: block; }
.lib-inspector h2, .lib-rename, .lib-rename-text, .lib-card .name { overflow-wrap: anywhere; word-break: break-word; max-width: 100%; }
.lib-inspector input, .lib-inspector select, .lib-inspector button, .lib-inspector form { max-width: 100%; min-width: 0; }
.lib-rename { display: flex; gap: 8px; align-items: flex-start; min-width: 0; }
.lib-rename-text { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.lib-rename-edit { width: auto; flex: none; white-space: nowrap; padding: 4px 8px; }
.lib-rename-input { width: 100%; max-width: 100%; min-width: 12rem; }
.lib-rename:has(.lib-rename-input) { flex-direction: column; align-items: stretch; }
.lib-rename-error { color: var(--danger); font-size: 13px; }
.lib-switch { display: flex; gap: 6px; align-items: center; padding: 6px 8px; border-radius: 10px; text-decoration: none; color: var(--ink); }
.lib-switch.active { background: var(--hover); font-weight: 600; }
.lib-badge { font-size: 12px; color: var(--muted); border: 1px solid var(--line); border-radius: 999px; padding: 0 6px; }
.lib-nav, .lib-tree { display: grid; gap: 2px; }
.lib-nav a, .lib-tree a { display: flex; justify-content: space-between; gap: 8px; padding: 6px 8px; border-radius: 8px; text-decoration: none; color: var(--ink); }
.lib-nav a.active, .lib-tree a.active { background: var(--hover); }
.lib-count { color: var(--muted); font-size: 12px; }
.lib-section { border-top: 1px solid var(--line); padding-top: 8px; }
.lib-section summary { display: flex; justify-content: space-between; align-items: center; cursor: pointer; font-weight: 600; }
.lib-add { width: 28px; height: 28px; border-radius: 8px; padding: 0; }
.lib-settings summary { cursor: pointer; color: var(--muted); }
.lib-toolbar { display: flex; flex-wrap: nowrap; gap: 8px; align-items: center; margin-bottom: 12px; min-width: 0; }
.lib-toolbar form { display: flex; flex-wrap: nowrap; gap: 8px; align-items: center; margin: 0; min-width: 0; }
.lib-toolbar input[name="q"] { width: 8rem; min-width: 0; flex: 1; }
.lib-toolbar label { display: flex; flex: 0 0 auto; flex-direction: row; align-items: center; gap: 6px; white-space: nowrap; }
.lib-toolbar form:first-child { flex: 1 1 auto; min-width: 0; }
.lib-import { margin-left: auto; flex: 0 0 auto; }
.lib-import input[type=file] { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); border: 0; }
.lib-import label.button { border: 1px solid var(--line); border-radius: 10px; padding: 6px 12px; cursor: pointer; background: var(--panel); }
.drop-zone { display: grid; place-items: center; gap: 8px; min-height: 220px; margin-bottom: 12px; border: 2px dashed var(--line); border-radius: 16px; color: var(--muted); text-align: center; }
.lib-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(148px, 100%), 1fr)); gap: 12px; min-height: 120px; }
.lib-card { position: relative; display: grid; gap: 6px; align-content: start; background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 8px; text-decoration: none; color: var(--ink); min-width: 0; }
.lib-card.selected { outline: 2px solid var(--accent); background: color-mix(in srgb, var(--accent) 12%, var(--panel)); }
.lib-check { position: absolute; top: 8px; right: 8px; z-index: 2; width: 22px; height: 22px; margin: 0; opacity: 0; }
.lib-card:hover .lib-check, .library.selecting .lib-check, .lib-card.selected .lib-check { opacity: 1; }
.lib-marquee { position: fixed; z-index: 4; border: 1px solid var(--accent); background: color-mix(in srgb, var(--accent) 18%, transparent); pointer-events: none; }
.lib-batch { position: sticky; bottom: 8px; z-index: 3; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; max-width: 100%; margin-top: 12px; padding: 8px; border: 1px solid var(--line); border-radius: 12px; background: var(--panel); }
.lib-batch[hidden], #lib-multi[hidden], #lib-single[hidden] { display: none; }
.lib-dialog { border: 1px solid var(--line); border-radius: 16px; padding: 16px; background: var(--bg); color: var(--ink); max-width: min(420px, 100%); }
.lib-dialog::backdrop { background: rgba(0, 0, 0, .35); }
.lib-card .name { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; min-height: 2.6em; line-height: 1.3; }
.lib-avatar, .lib-mini { width: 22px; height: 22px; border-radius: 999px; background: var(--accent); color: var(--on-accent); font-size: 11px; display: inline-grid; place-items: center; }
.lib-avatar { position: absolute; top: 12px; left: 12px; }
.lib-card img, .lib-thumb { width: 100%; height: 112px; object-fit: contain; background: var(--hover); border-radius: 8px; }
.lib-preview { width: auto; max-width: 100%; height: auto; max-height: 220px; object-fit: contain; background: var(--hover); border-radius: 12px; }
.type-tile { display: grid; place-items: center; height: 112px; background: var(--hover); border-radius: 8px; font-weight: 600; }
.type-tile.modest { width: 120px; height: 150px; }
.lib-stars { display: flex; gap: 4px; }
.lib-stars button { width: 32px; padding: 4px 0; }
.lib-stars .on { color: var(--warn); }
.lib-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.lib-chip { border: 1px solid var(--line); border-radius: 999px; padding: 2px 8px; font-size: 12px; }
.lib-actions { display: grid; gap: 8px; margin-top: 8px; }
@media (max-width: 1100px) {
  .library, .library.has-selection { grid-template-columns: minmax(0, 180px) minmax(0, 1fr); }
  .lib-inspector { grid-column: 1 / -1; }
}
@media (max-width: 800px) { .library, .library.has-selection { grid-template-columns: minmax(0, 1fr); } }
.file-list { display: grid; gap: 12px; }
.file-card { min-width: 0; background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 12px; }
.file-card input { width: 100%; }
.file-card img, img.preview { width: 100%; max-height: 160px; object-fit: cover; border-radius: 8px; }
iframe.preview { width: 100%; height: 360px; border: 1px solid var(--line); border-radius: 12px; }
.file-card form { margin-top: 8px; }
.type-badge { display: inline-block; width: max-content; font-size: 12px; color: var(--muted); border: 1px solid var(--line); border-radius: 999px; padding: 0 8px; }
.technical { margin-top: 12px; color: var(--muted); }
.svc-grid { display: grid; gap: 16px; }
.svc { display: grid; grid-template-columns: auto 1fr; gap: 12px; align-items: start; background: var(--panel); border: 1px solid var(--line); border-radius: 16px; padding: 16px; }
.svc .body { display: grid; gap: 8px; min-width: 0; }
.svc-head { display: flex; justify-content: space-between; gap: 12px; align-items: center; flex-wrap: wrap; }
.svc form { display: flex; flex-wrap: wrap; gap: 8px; align-items: end; margin: 0; }
.consent { display: grid; gap: 8px; }
a.button { display: inline-flex; align-items: center; width: auto; border-radius: 12px; padding: 8px 12px; border: 1px solid transparent; }
a.button.primary { background: var(--accent); }
.manage summary { padding: 8px 0; }
.dot { width: 8px; height: 8px; border-radius: 99px; background: #737373; }
.dot.on { background: #15803d; }
.dot.warn { background: #b45309; }
table { width: 100%; border-collapse: collapse; background: var(--panel); }
td, th { text-align: left; vertical-align: top; padding: 8px; border-bottom: 1px solid var(--line); }
body.sidebar-closed .sidebar { display: none; }
@media (max-width: 800px) {
  .menu-btn { display: inline-flex; }
  .sidebar { display: none; position: fixed; inset: 0 auto 0 64px; z-index: 4; width: min(260px, 86vw); }
  body.sidebar-open .sidebar { display: flex; }
  .info { display: none; }
  body.info-open .info { display: block; position: fixed; inset: 0 0 0 64px; z-index: 5; width: auto; }
  .rail { width: 52px; }
}
"""


def _asset_hash(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))[:12]


STYLE_PATH = "/ui/assets/app-" + _asset_hash(APP_CSS) + ".css"
NOTICES_JS = r"""
(function () {
  var count = document.getElementById("notice-count");
  var banner = document.getElementById("notice-banner");
  if (!count && !banner) return;
  function paint(data) {
    var n = Number(data && data.count) || 0;
    var item = (data && data.items && data.items[0]) || {};
    if (count) {
      count.hidden = n < 1;
      count.textContent = n ? String(n) : "";
    }
    var site = ((data && data.items) || []).filter(function (row) { return row && !row.due; })[0];
    if (site) item = site;
    if (banner) {
      banner.hidden = n < 1;
      if (!n) banner.textContent = "";
      else if (item.due) banner.textContent = "Reminder: " + (item.text || "") + " was due " + item.due;
      else banner.textContent = item.text || "";
    }
  }
  function poll() {
    fetch("/ui/notices", {headers: {"Accept": "application/json"}, credentials: "same-origin"})
      .then(function (response) { return response.ok ? response.json() : {count: 0, items: []}; })
      .then(paint)
      .catch(function () {});
  }
  poll();
  setInterval(poll, 15000);
  var search = document.getElementById("thread-search");
  if (search) {
    search.addEventListener("input", function () {
      var needle = search.value.trim().toLowerCase();
      document.querySelectorAll(".conv, .pin").forEach(function (row) {
        var title = (row.getAttribute("data-title") || row.textContent || "").toLowerCase();
        row.hidden = needle && title.indexOf(needle) < 0;
      });
    });
  }
  document.querySelectorAll(".tabs [role=tab]").forEach(function (tab) {
    tab.addEventListener("click", function () {
      document.querySelectorAll(".tabs [role=tab]").forEach(function (other) {
        var on = other === tab;
        other.setAttribute("aria-selected", String(on));
        var panel = document.getElementById(other.getAttribute("aria-controls"));
        if (panel) panel.hidden = !on;
      });
    });
  });
  var infoToggle = document.getElementById("info-toggle");
  if (infoToggle) {
    infoToggle.addEventListener("click", function () {
      var open = document.body.classList.toggle("info-open");
      infoToggle.setAttribute("aria-expanded", String(open || window.matchMedia("(min-width: 801px)").matches));
    });
  }
})();
"""
NOTICES_PATH = "/ui/assets/notices-" + _asset_hash(NOTICES_JS) + ".js"
CONN_JS = """
"use strict";
document.querySelectorAll("[data-copy]").forEach(function (button) {
  button.addEventListener("click", function () {
    var value = button.getAttribute("data-copy") || "";
    function mark() { button.textContent = "Copied"; }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(value).then(mark).catch(mark);
      return;
    }
    mark();
  });
});
"""
CONN_PATH = "/ui/assets/conn-" + _asset_hash(CONN_JS) + ".js"
_ASSET_CACHE = "public, max-age=31536000, immutable"


def _style_link() -> str:
    return f'<link rel="stylesheet" href="{STYLE_PATH}">'


def _static_asset(method: str, path: str) -> Page | None:
    if method != "GET":
        return None
    if path == STYLE_PATH:
        return Page(200, APP_CSS, content_type="text/css; charset=utf-8", cache_control=_ASSET_CACHE)
    if path == NOTICES_PATH:
        return Page(200, NOTICES_JS, content_type="text/javascript; charset=utf-8", cache_control=_ASSET_CACHE)
    from ssios.library_page import LIBRARY_JS, library_script_path

    if path == library_script_path():
        return Page(200, LIBRARY_JS, content_type="text/javascript; charset=utf-8", cache_control=_ASSET_CACHE)
    if path.startswith("/ui/assets/chat-") and path.endswith(".js"):
        from ssios.ui_household import CHAT_JS, CHAT_PATH

        if path == CHAT_PATH:
            return Page(200, CHAT_JS, content_type="text/javascript; charset=utf-8", cache_control=_ASSET_CACHE)
    if path == CONN_PATH:
        return Page(200, CONN_JS, content_type="text/javascript; charset=utf-8", cache_control=_ASSET_CACHE)
    vendor = _vendor_asset(path)
    if vendor is not None:
        return vendor
    return None


def _vendor_asset(path: str) -> Page | None:
    """Same-origin KaTeX and the markdown renderer. No CDN."""
    prefix = "/ui/vendor/"
    if not path.startswith(prefix):
        return None
    rel = path[len(prefix):]
    if not rel or ".." in rel.split("/") or rel.startswith("/"):
        return None
    allowed = rel in {"render.js", "katex/katex.min.js", "katex/katex.min.css"} or (
        rel.startswith("katex/fonts/") and rel.endswith(".woff2") and rel.count("/") == 2
    )
    if not allowed:
        return None
    root = Path(__file__).resolve().parent / "vendor"
    file = (root / rel).resolve()
    if root not in file.parents or not file.is_file():
        return None
    kinds = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".woff2": "font/woff2"}
    return Page(200, file.read_bytes(), content_type=kinds[file.suffix], cache_control=_ASSET_CACHE)


def _login_page(code: str) -> str:
    value = html.escape(code)
    return (
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><title>Sign in</title>"
        + _style_link()
        + "<body><main><h1>Sign in</h1><form method=\"post\" action=\"/ui/login\">"
        "<label>Username <input name=\"username\" autocomplete=\"username\"></label>"
        "<label>Password <input name=\"password\" type=\"password\" autocomplete=\"current-password\"></label>"
        f"<label>One-time code <input name=\"code\" value=\"{value}\"></label>"
        "<button class=\"primary\" type=\"submit\">Sign in</button></form>"
        "<p><a href=\"/ui/signup\">Request an account</a></p>"
        "<p>The first admin is created on the host with <code>ssios user add --admin</code>. There is no default password.</p>"
        "</main></body></html>"
    )


def icon(name: str) -> str:
    """A small inline icon. The link or button carries the accessible name."""
    paths = {
        "chat": '<path d="M5 6h14v9H8l-3 3z"/>',
        "search": '<circle cx="11" cy="11" r="6"/><path d="M16 16l4 4"/>',
        "notes": '<path d="M7 4h8l4 4v12H7z"/><path d="M15 4v4h4"/>',
        "files": '<path d="M4 7h6l2 2h8v9H4z"/>',
        "memory": '<path d="M12 4a5 5 0 0 1 5 5c0 4-5 11-5 11S7 13 7 9a5 5 0 0 1 5-5z"/>',
        "tasks": '<rect x="5" y="5" width="14" height="14" rx="3"/><path d="M8 12l2.5 2.5L16 9"/>',
        "link": '<path d="M10 13a4 4 0 0 0 6 0l2-2a4 4 0 0 0-6-6l-1 1"/><path d="M14 11a4 4 0 0 0-6 0l-2 2a4 4 0 0 0 6 6l1-1"/>',
        "plus": '<path d="M12 5v14M5 12h14"/>',
        "settings": '<circle cx="12" cy="12" r="3"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6 17 7M7 17l-1.4 1.4"/>',
        "logout": '<path d="M9 6H6v12h3"/><path d="M10 12h9M16 8l4 4-4 4"/>',
        "admin": '<path d="M12 3l7 3v6c0 4-3 7-7 9-4-2-7-5-7-9V6z"/>',
        "bell": '<path d="M6 16V11a6 6 0 1 1 12 0v5l1 2H5z"/><path d="M10 19a2 2 0 0 0 4 0"/>',
        "repeat": '<path d="M7 7h9l-2-2M17 17H8l2 2"/><path d="M17 7v4M7 17v-4"/>',
        "check": '<circle cx="12" cy="12" r="8"/><path d="M8 12l2.5 2.5L16 9"/>',
        "mic": '<rect x="9" y="4" width="6" height="10" rx="3"/><path d="M6 11a6 6 0 0 0 12 0M12 17v3"/>',
        "paperclip": '<path d="M8 12l6-6a3 3 0 0 1 4 4l-7 7a4 4 0 0 1-6-6l7-7"/>',
        "person": '<circle cx="12" cy="8" r="3"/><path d="M6 19c1.2-3 3.2-4.5 6-4.5S16.8 16 18 19"/>',
        "people": '<circle cx="9" cy="8" r="2.5"/><circle cx="16" cy="9" r="2"/><path d="M4.5 18c.8-2.2 2.2-3.4 4.5-3.4s3.7 1.2 4.5 3.4"/><path d="M13 14.7c.8-.4 1.7-.6 2.7-.6 1.7 0 3 .9 3.8 2.6"/>',
    }
    body = paths.get(name, paths["chat"])
    return (
        '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="none" '
        'stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">'
        + body + "</svg>"
    )


def _rail(session: dict) -> str:
    csrf = html.escape(str(session.get("csrf") or ""))
    account = session.get("account") if isinstance(session.get("account"), dict) else {}
    admin = f'<a href="/ui/admin" title="Admin" aria-label="Admin">{icon("admin")}</a>' if account.get("role") == "admin" else ""
    return (
        '<nav class="rail" aria-label="Sections">'
        f'<a href="/ui/chat" title="Chat" aria-label="Chat">{icon("chat")}</a>'
        f'<a href="#thread-search" title="Search" aria-label="Search">{icon("search")}</a>'
        f'<a href="/ui/vault" title="Notes" aria-label="Notes">{icon("notes")}</a>'
        f'<a href="/ui/library" title="Files" aria-label="Files">{icon("files")}</a>'
        f'<a href="/ui/memory?project_id=local-summary" title="Memory" aria-label="Memory">{icon("memory")}</a>'
        f'<a href="/ui/tasks" title="Tasks" aria-label="Tasks">{icon("tasks")}</a>'
        f'<a href="/ui/connections" title="Connections" aria-label="Connections">{icon("link")}</a>'
        f'<a class="new-chat" href="/ui/chat" title="New chat" aria-label="New chat"><span class="sr">New chat</span>{icon("plus")}</a>'
        '<span class="rail-gap"></span>'
        f'<a href="/ui/settings" title="Settings" aria-label="Settings">{icon("settings")}</a>'
        + admin
        + f'<form method="post" action="/ui/logout"><input type="hidden" name="csrf" value="{csrf}">'
        f'<button type="submit" title="Sign out" aria-label="Sign out">{icon("logout")}</button></form>'
        "</nav>"
    )


def _shell(session: dict, main: str, conversations: str = "") -> str:
    csrf = html.escape(str(session.get("csrf") or ""))
    account = session.get("account") if isinstance(session.get("account"), dict) else {}
    who = html.escape(str(account.get("username") or ""))
    initial = html.escape((str(account.get("username") or "?")[:1] or "?").upper())
    admin = "<a href=\"/ui/admin\">Admin</a>" if account.get("role") == "admin" else ""
    threads = conversations or (
        "<section><h2>Main</h2><a class=\"pin\" href=\"/ui/chat\">Main chat</a></section>"
        "<section><h2>Channels</h2><p class=\"muted\">No channels yet.</p></section>"
        "<section class=\"side-head\"><h2>Side chats</h2><a class=\"plus\" href=\"/ui/chat\" aria-label=\"New chat\">+</a></section>"
    )
    return (
        "<div class=\"app\">" + _rail(session) + "<aside id=\"sidebar\" class=\"sidebar\">"
        "<a class=\"brand\" href=\"/ui/chat\">SSIOS</a>"
        "<label class=\"finder\">Search<input id=\"thread-search\" type=\"search\" placeholder=\"Search\" aria-label=\"Search threads\"></label>"
        + threads
        + "<div class=\"dock\"><details class=\"account\"><summary><span class=\"account-row\"><span class=\"avatar\">"
        + initial
        + "</span><span>Signed in as "
        + who
        + "</span></span></summary><div class=\"popover\" role=\"menu\">"
        "<a href=\"/ui/connections\">Connections</a>"
        "<a href=\"/ui/memory?project_id=local-summary\">Memory</a>"
        "<a href=\"/ui/vault\">Notes</a>"
        "<a href=\"/ui/tasks\">Tasks <span id=\"notice-count\" class=\"badge\" hidden></span></a>"
        "<a href=\"/ui/settings\">Settings</a>"
        + admin
        + "<p class=\"menu-label\">More</p>"
        "<a href=\"/ui\">Console</a><a href=\"/ui/tasks\">Tasks</a><a href=\"/ui/audit\">Activity</a>"
        "<a href=\"/ui/ingest\">Ingest</a><a href=\"/ui/summary\">Summary</a><a href=\"/ui/approvals\">Approvals</a>"
        + f"<form method=\"post\" action=\"/ui/logout\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        "<button type=\"submit\">Sign out</button></form></div></details></div></aside>"
        "<div class=\"stage\"><p id=\"notice-banner\" class=\"notice\" hidden></p>"
        "<button type=\"button\" class=\"menu-btn\" id=\"sidebar-toggle\" aria-controls=\"sidebar\" aria-expanded=\"true\">Menu</button>"
        + main
        + "</div></div>"
    )


def _page(session: dict, title: str, body: str, *, chat: bool = False, conversations: str = "", wide: bool = False, bleed: bool = False) -> str:
    sheet = "sheet wide" if wide else "sheet"
    if bleed:
        main = f'<div class="page page-bleed"><h1>{html.escape(title)}</h1>{body}</div>'
    else:
        main = body if chat else f"<div class=\"page\"><div class=\"{sheet}\"><h1>{html.escape(title)}</h1>{body}</div></div>"
    return (
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>" + html.escape(title) + "</title>" + _style_link()
        + "<body class=\"app-body\">" + _shell(session, main, conversations)
        + f"<script src=\"{NOTICES_PATH}\"></script></body></html>"
    )
