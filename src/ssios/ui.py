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
from ssios.util import read_json, secure_mkdir, write_json

SESSION_COOKIE = "ssios_session"
CODE_TTL = 120
SESSION_TTL = 12 * 3600


class Page:
    def __init__(self, status: int, body: str, *, cookie: str | None = None) -> None:
        self.status = status
        self.body = body
        self.cookie = cookie


def handle(plane, method: str, target: str, headers, body: dict) -> Page:
    parts = urlsplit(target)
    path = parts.path.rstrip("/") or "/ui"
    query = {key: values[-1] for key, values in parse_qs(parts.query).items()}
    rejected = _check_origin(plane, headers)
    if rejected is not None:
        return rejected
    if method == "GET" and path == "/ui/login":
        return Page(200, _login_page(query.get("code", "")))
    if method == "POST" and path == "/ui/login":
        return _login(plane, body)
    session = _session(plane, headers)
    if session is None:
        return Page(401, _login_page(""))
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
        return _decide(plane, session, path.rsplit("/", 1)[-1], body)
    if method == "GET" and path == "/ui/ingest":
        return Page(200, _ingest_form(plane, session, ""))
    if method == "POST" and path == "/ui/ingest":
        _csrf(session, body)
        return _ingest(plane, session, body)
    if method == "GET" and path == "/ui/summary":
        return Page(200, _summary_form(plane, session))
    if method == "POST" and path == "/ui/summary":
        _csrf(session, body)
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
    if method == "GET" and path == "/ui/memory":
        return Page(200, _memory(plane, session, query.get("project_id", "local-summary"), query.get("path", "")))
    if method == "GET" and path == "/ui/settings":
        return Page(200, _settings(plane, session))
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


def _login(plane, body: dict) -> Page:
    token = str(body.get("token") or "")
    code = str(body.get("code") or "")
    if code and _take_code(plane, code):
        return _start_session(plane)
    expected = plane.auth.read_secret("human")
    if token and hmac.compare_digest(token, expected):
        return _start_session(plane)
    return Page(401, _login_page(""))


def _start_session(plane) -> Page:
    session_id = secrets.token_urlsafe(24)
    csrf = secrets.token_urlsafe(18)
    path = _ui_dir(plane) / "sessions.json"
    current = read_json(path, {"sessions": {}})
    if not isinstance(current, dict):
        current = {"sessions": {}}
    sessions = current.get("sessions") if isinstance(current.get("sessions"), dict) else {}
    sessions[session_id] = {"csrf": csrf, "expires": time.time() + SESSION_TTL}
    write_json(path, {"sessions": sessions}, 0o600)
    cookie = f"{SESSION_COOKIE}={session_id}; HttpOnly; SameSite=Strict; Path=/ui"
    return Page(200, _page({"csrf": csrf}, "Signed in", "<p>Signed in. The token was not written to the session file.</p><p><a href=\"/ui\">Open the console</a></p>"), cookie=cookie)


def _session(plane, headers) -> dict | None:
    jar = SimpleCookie(headers.get("Cookie", ""))
    morsel = jar.get(SESSION_COOKIE)
    if morsel is None or not morsel.value:
        return None
    current = read_json(_ui_dir(plane) / "sessions.json", {"sessions": {}})
    sessions = current.get("sessions") if isinstance(current, dict) else {}
    record = sessions.get(morsel.value) if isinstance(sessions, dict) else None
    if not isinstance(record, dict) or record.get("expires", 0) < time.time():
        return None
    return {"id": morsel.value, "csrf": record.get("csrf", "")}


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
    path = _ui_dir(plane) / "sessions.json"
    current = read_json(path, {"sessions": {}})
    sessions = current.get("sessions") if isinstance(current, dict) else {}
    if isinstance(sessions, dict):
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


def _check_origin(plane, headers) -> Page | None:
    """Reject cross-site UI posts. A literal Origin of null falls back to Referer."""
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
        + _STYLE
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


def _tasks(plane, session: dict) -> str:
    rows = []
    for task in plane.tasks.values():
        rows.append(
            f"<li><a href=\"/ui/tasks/{html.escape(task['id'])}\">{html.escape(task['id'])}</a> {html.escape(str(task.get('status')))}</li>"
        )
    return _page(session, "Tasks", "<ul>" + "".join(rows) + "</ul>")


def _task(plane, session: dict, task_id: str) -> str:
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
    rows = []
    token = _human(plane)
    for record in plane.approvals.records():
        if record.get("status") != "pending":
            continue
        preview = plane.get_approval(token, record["approval_id"])
        files = preview.get("files") or []
        diff = html.escape("\n\n".join(str(item.get("content") or "") for item in files))
        rows.append(
            "<article><h2>"
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
        "<button name=\"decision\" value=\"approve\" type=\"submit\">Approve</button>"
        "<button name=\"decision\" value=\"deny\" type=\"submit\">Deny</button></form>"
    )


def _decide(plane, session: dict, approval_id: str, body: dict) -> Page:
    result = plane.decide(_human(plane), approval_id, body)
    return Page(200, _page(session, "Decided", f"<p>{html.escape(str(result.get('status')))}</p>"))


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


def _settings(plane, session: dict) -> str:
    status = plane.status(_human(plane))
    safe = {
        "backend": status.get("runtime", {}).get("active_backend"),
        "isolated": status.get("runtime", {}).get("isolated"),
        "memory": status.get("memory", {}).get("mode"),
        "model": status.get("llm", {}).get("model"),
    }
    return _page(session, "Settings", f"<pre>{html.escape(json.dumps(safe, ensure_ascii=False, indent=2))}</pre>")


_STYLE = """
<style>
body { margin: 0; font: 16px/1.5 Georgia, "Iowan Old Style", serif; color: #1c1915; background: #f6f1e7; }
nav, main { max-width: 46rem; margin: 0 auto; }
nav { display: flex; gap: 1rem; padding: 1rem 0; }
a { color: #6b3a1f; }
h1 { font-weight: 500; }
form { display: grid; gap: 0.75rem; }
input, textarea, button { font: inherit; padding: 0.45rem 0.6rem; }
button { background: #1c1915; color: #f6f1e7; border: 0; width: max-content; }
pre { white-space: pre-wrap; background: #fff; padding: 1rem; }
article { border-top: 1px solid #d9d0c1; padding-top: 1rem; }
</style>
"""


def _login_page(code: str) -> str:
    value = html.escape(code)
    return (
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><title>Sign in</title>"
        + _STYLE
        + "<body><main><h1>Sign in</h1><form method=\"post\" action=\"/ui/login\">"
        f"<label>One-time code <input name=\"code\" value=\"{value}\"></label>"
        "<label>Human token <input name=\"token\" type=\"password\"></label>"
        "<button type=\"submit\">Sign in</button></form></main></body></html>"
    )


def _page(session: dict, title: str, body: str) -> str:
    csrf = html.escape(str(session.get("csrf") or ""))
    return (
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><title>"
        + html.escape(title)
        + "</title>"
        + _STYLE
        + "<body><nav><a href=\"/ui\">Console</a> <a href=\"/ui/tasks\">Tasks</a> "
        "<a href=\"/ui/approvals\">Approvals</a> <a href=\"/ui/ingest\">Ingest</a> <a href=\"/ui/summary\">Summary</a> "
        "<a href=\"/ui/memory?project_id=local-summary\">Memory</a> <a href=\"/ui/settings\">Settings</a></nav>"
        f"<main><h1>{html.escape(title)}</h1>{body}"
        f"<form method=\"post\" action=\"/ui/logout\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        "<button type=\"submit\">Sign out</button></form></main></body></html>"
    )
