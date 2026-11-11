"""Local web UI. The human token is checked once and is not stored in the session."""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import secrets
import time
from http.cookies import SimpleCookie
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
    ) -> None:
        self.status = status
        self.body = body
        self.cookie = cookie
        self.cookies = list(cookies or ([] if not cookie else [cookie]))
        self.stream = stream
        self.content_type = content_type
        self.location = location
        self.cache_control = cache_control


def handle(plane, method: str, target: str, headers, body: dict, peer_ip: str = "127.0.0.1") -> Page:
    parts = urlsplit(target)
    path = parts.path.rstrip("/") or "/ui"
    query = {key: values[-1] for key, values in parse_qs(parts.query).items()}
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
    if method == "GET" and path == "/ui/tasks":
        return Page(200, _tasks(plane, session))
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
        return Page(200, _memory(plane, session, query.get("project_id", "local-summary"), query.get("path", "")))
    if method == "GET" and path == "/ui/settings":
        return Page(200, _settings(plane, session))
    if method == "POST" and path == "/ui/settings":
        _csrf(session, body)
        return _settings_post(plane, session, body)
    if method == "GET" and path == "/ui/vault":
        return Page(200, _vault_page(plane, session, ""))
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


def browser_error_page(detail: str, status: int) -> Page:
    """A normal page for a browser. API clients do not use this."""
    safe = html.escape(detail or "The page could not be shown.")
    body = (
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><title>Something went wrong</title>"
        + _style_link()
        + "<body><main><h1>Something went wrong</h1><p>"
        + safe
        + "</p><p><a href=\"/ui/login\">Sign in</a> <a href=\"/ui/chat\">Chat</a> "
        "<a href=\"/ui/connections\">Connections</a></p></main></body></html>"
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


def _tasks(plane, session: dict) -> str:
    from ssios.scope import AccountScope

    account = _account(session)
    scope = AccountScope(plane, account)
    rows = []
    if account.get("legacy_owner"):
        for task in plane.tasks.values():
            rows.append(
                f"<li><a href=\"/ui/tasks/{html.escape(task['id'])}\">{html.escape(task['id'])}</a> {html.escape(str(task.get('status')))}</li>"
            )
    for task in scope.tasks().values():
        rows.append(
            f"<li><a href=\"/ui/tasks/{html.escape(str(task['id']))}\">{html.escape(str(task['id']))}</a> {html.escape(str(task.get('status')))}</li>"
        )
    return _page(session, "Tasks", "<ul>" + "".join(rows) + "</ul>")


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
    google_record = _google_fetch_record(plane, approval_id)
    if google_record is not None:
        return _decide_google(plane, session, scope, google_record, body, peer_ip)
    if scope.has_approval(approval_id):
        result = scope.decide(approval_id, body, peer_ip)
        return Page(200, _page(session, "Decided", f"<p>{html.escape(str(result.get('status')))}</p>"))
    if account.get("legacy_owner"):
        result = plane.decide(_human(plane), approval_id, body)
        return Page(200, _page(session, "Decided", f"<p>{html.escape(str(result.get('status')))}</p>"))
    raise SsiosError("not_found", "approval not found", 404)


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


def _memory(plane, session: dict, project_id: str, path: str) -> str:
    account = _account(session)
    if not account.get("legacy_owner"):
        from ssios.scope import AccountScope

        scope = AccountScope(plane, account)
        project_id = scope.project_id()
        paths = scope.memory().list_markdown(project_id)
        links = "".join(
            f"<li><a href=\"/ui/memory?path={html.escape(item)}\">{html.escape(item)}</a></li>"
            for item in paths
        )
        content = ""
        if path:
            doc = scope.memory().read_file(project_id, path, include_untrusted=False)
            content = f"<pre>{html.escape(str(doc.get('content') or ''))}</pre>"
        return _page(session, "Memory", f"<ul>{links}</ul>{content}")
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


def _vault_page(plane, session: dict, notice: str) -> str:
    from ssios.vault import list_notes

    notes = list_notes(plane.settings, session["account"]["id"])
    items = []
    for item in notes:
        path = html.escape(item["path"], quote=True)
        title = html.escape(item["title"])
        source = html.escape(item.get("source") or "")
        items.append(f'<li><a href="/ui/vault/note?path={path}">{title}</a> <span>{source}</span></li>')
    body = notice + "<p>These notes are on this computer. Open the vault folder in Obsidian.</p><ul>" + ("".join(items) or "<li>No notes yet.</li>") + "</ul>"
    return _page(session, "Notes", body)


def _vault_note(plane, session: dict, rel: str) -> str:
    from urllib.parse import quote

    from ssios.vault import read_note, render_markdown

    text = read_note(plane.settings, session["account"]["id"], rel)

    def link_for(url: str) -> str:
        if url.startswith(("javascript:", "data:")):
            return ""
        if url.startswith(("https://", "http://")):
            return url
        if url.startswith("/") or ".." in url.split("/"):
            return ""
        if url.endswith(".md"):
            return "/ui/vault/note?path=" + quote("Clippings/" + url)
        return "/ui/vault/asset?path=" + quote("Clippings/" + url)

    meta, rendered = render_markdown(text, link_for=link_for)
    props = "".join(f"<dt>{html.escape(key)}</dt><dd>{html.escape(value)}</dd>" for key, value in meta.items())
    return _page(session, "Note", f"<dl>{props}</dl>{rendered}<p><a href=\"/ui/vault\">All notes</a></p>")


def _vault_asset(plane, session: dict, rel: str) -> Page:
    from ssios.vault import read_asset

    data, content_type = read_asset(plane.settings, session["account"]["id"], rel)
    return Page(200, data, content_type=content_type)


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
        f"<input type=\"hidden\" name=\"auto_save\" value=\"{'off' if prefs.get('auto_save') else 'on'}\">"
        f"<button type=\"submit\">{'Turn auto-save off' if prefs.get('auto_save') else 'Turn auto-save on'}</button></form>"
        "<p>Auto-save is off by default. A clip is written to your vault only after you approve it.</p>"
        f"<form method=\"post\" action=\"/ui/settings\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        f"<label>Time zone <input name=\"time_zone\" value=\"{html.escape(str((plane.accounts.users.get(session['account']['id']) or {}).get('time_zone') or ''))}\" placeholder=\"America/Los_Angeles\"></label> "
        "<button type=\"submit\">Save time zone</button></form>"
        "</section>"
    )
    return _page(session, "Settings", form + f"<pre>{html.escape(json.dumps(safe, ensure_ascii=False, indent=2))}</pre>")


def _settings_post(plane, session: dict, body: dict) -> Page:
    account_id = session["account"]["id"]
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
    elif location is not None:
        plane.skills.set_account(account_id, location=str(location))
    return Page(200, _settings(plane, session))


APP_CSS = """
:root {
  color-scheme: light dark;
  --bg: #fafafa; --ink: #171717; --muted: #525252; --panel: #ffffff; --line: #e7e7e7;
  --user: #ececec; --accent: #1d4ed8; --on-accent: #ffffff;
  --danger: #b91c1c; --danger-bg: #fef2f2; --ok: #166534; --warn: #92400e; --warn-bg: #fef3c7; --ok-bg: #dcfce7;
  --code: #f4f4f5;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f0f10; --ink: #f5f5f5; --muted: #a3a3a3; --panel: #171717; --line: #2a2a2a;
    --user: #2a2a2e; --accent: #93c5fd; --on-accent: #0f0f10;
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
button.primary { background: var(--accent); color: var(--on-accent); border-color: transparent; }
button:disabled { opacity: 0.45; cursor: not-allowed; }
button.danger, .card.danger button.primary { background: var(--danger); color: #fff; border-color: transparent; }
@media (prefers-color-scheme: dark) {
  button.danger, .card.danger button.primary { color: #3f1515; }
}
label { display: grid; gap: 4px; }
form { display: grid; gap: 12px; }
pre, code { font-family: ui-monospace, "SF Mono", "Sarasa Mono SC", monospace; }
.app { height: 100%; display: flex; min-height: 0; }
.sidebar { width: 260px; flex: none; display: flex; flex-direction: column; border-right: 1px solid var(--line); padding: 16px 12px; background: var(--bg); }
.brand { font-weight: 650; color: var(--ink); text-decoration: none; }
.new-chat { display: block; margin: 16px 0; text-align: center; text-decoration: none; border: 1px solid var(--line); border-radius: 12px; padding: 8px 12px; color: var(--ink); }
.convs { flex: 1; overflow: auto; min-height: 0; }
.convs h2 { font-size: 12px; font-weight: 600; color: var(--muted); margin: 16px 12px 4px; }
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
.menu-btn { display: none; margin: 8px 16px 0; width: max-content; }
.thread { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.log { flex: 1; overflow: auto; min-height: 0; }
.column { max-width: 760px; margin: 0 auto; padding: 24px 16px 8px; display: flex; flex-direction: column; gap: 16px; }
.msg { display: flex; gap: 8px; }
.msg.user { justify-content: flex-end; }
.msg.user .bubble { max-width: 75%; background: var(--user); border-radius: 16px; padding: 8px 12px; }
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
.composer { padding: 8px 16px 16px; }
.composer-box { max-width: 760px; margin: 0 auto; border: 1px solid var(--line); border-radius: 16px; background: var(--panel); padding: 8px 8px 8px 16px; box-shadow: 0 8px 24px rgba(0,0,0,.04); display: grid; gap: 8px; }
.composer-box textarea { border: 0; resize: none; min-height: 24px; max-height: 160px; padding: 8px 0; background: transparent; width: 100%; outline: none; }
.composer-bar { display: flex; justify-content: space-between; align-items: center; }
.route-chip select { border: 0; background: transparent; padding: 4px 8px; }
.send { width: 36px; height: 36px; border-radius: 999px; display: grid; place-items: center; padding: 0; }
.send:disabled { opacity: .4; }
.hint { max-width: 760px; margin: 8px auto 0; color: var(--muted); font-size: 12px; }
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
.steps { list-style: none; padding: 0; margin: 0; display: flex; gap: 12px; color: var(--muted); font-size: 13px; }
.steps .done::before { content: "\\2713  "; color: var(--ok); }
.steps .spin::before { content: "\\25CC  "; }
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
  .sidebar { display: none; position: fixed; inset: 0 auto 0 0; z-index: 4; width: min(260px, 86vw); }
  body.sidebar-open .sidebar { display: flex; }
}
"""


def _asset_hash(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))[:12]


STYLE_PATH = "/ui/assets/app-" + _asset_hash(APP_CSS) + ".css"
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
    if path.startswith("/ui/assets/chat-") and path.endswith(".js"):
        from ssios.ui_household import CHAT_JS, CHAT_PATH

        if path == CHAT_PATH:
            return Page(200, CHAT_JS, content_type="text/javascript; charset=utf-8", cache_control=_ASSET_CACHE)
    if path == CONN_PATH:
        return Page(200, CONN_JS, content_type="text/javascript; charset=utf-8", cache_control=_ASSET_CACHE)
    return None


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


def _shell(session: dict, main: str, conversations: str = "") -> str:
    csrf = html.escape(str(session.get("csrf") or ""))
    account = session.get("account") if isinstance(session.get("account"), dict) else {}
    who = html.escape(str(account.get("username") or ""))
    initial = html.escape((str(account.get("username") or "?")[:1] or "?").upper())
    admin = "<a href=\"/ui/admin\">Admin</a>" if account.get("role") == "admin" else ""
    return (
        "<div class=\"app\"><aside id=\"sidebar\" class=\"sidebar\">"
        "<a class=\"brand\" href=\"/ui/chat\">SSIOS</a>"
        "<a class=\"new-chat\" href=\"/ui/chat\">New chat</a>"
        + conversations
        + "<div class=\"dock\"><details class=\"account\"><summary><span class=\"account-row\"><span class=\"avatar\">"
        + initial
        + "</span><span>Signed in as "
        + who
        + "</span></span></summary><div class=\"popover\" role=\"menu\">"
        "<a href=\"/ui/connections\">Connections</a>"
        "<a href=\"/ui/memory?project_id=local-summary\">Memory</a>"
        "<a href=\"/ui/vault\">Notes</a>"
        "<a href=\"/ui/settings\">Settings</a>"
        + admin
        + "<p class=\"menu-label\">More</p>"
        "<a href=\"/ui\">Console</a><a href=\"/ui/tasks\">Tasks</a><a href=\"/ui/audit\">Activity</a>"
        "<a href=\"/ui/ingest\">Ingest</a><a href=\"/ui/summary\">Summary</a><a href=\"/ui/approvals\">Approvals</a>"
        + f"<form method=\"post\" action=\"/ui/logout\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        "<button type=\"submit\">Sign out</button></form></div></details></div></aside>"
        "<div class=\"stage\"><button type=\"button\" class=\"menu-btn\" id=\"sidebar-toggle\" aria-controls=\"sidebar\" aria-expanded=\"true\">Menu</button>"
        + main
        + "</div></div>"
    )


def _page(session: dict, title: str, body: str, *, chat: bool = False, conversations: str = "") -> str:
    main = body if chat else f"<div class=\"page\"><div class=\"sheet\"><h1>{html.escape(title)}</h1>{body}</div></div>"
    return (
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>" + html.escape(title) + "</title>" + _style_link()
        + "<body class=\"app-body\">" + _shell(session, main, conversations) + "</body></html>"
    )
