"""Localhost JSON API. Every route requires a bearer token."""

from __future__ import annotations

import html
import json
import re
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from ssios.config import lan_enabled
from ssios.errors import SsiosError
from ssios.netguard import is_loopback, origin_allowed, require_host, require_peer
from ssios.ui import Page, handle as handle_ui


def _multipart_memory(content_type: str, raw: bytes) -> dict:
    """Read a form upload into strings. The bytes are not written to a file."""
    match = re.search(r"boundary=([^;]+)", content_type)
    if match is None:
        raise SsiosError("bad_request", "upload rejected", 400)
    marker = match.group(1).strip().strip('"')
    if not marker or len(marker) > 200:
        raise SsiosError("bad_request", "upload rejected", 400)
    try:
        boundary = b"--" + marker.encode("ascii")
    except UnicodeError as exc:
        raise SsiosError("bad_request", "upload rejected", 400) from exc
    result: dict[str, str] = {}
    for chunk in raw.split(boundary):
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        if chunk.endswith(b"--"):
            chunk = chunk[:-2].rstrip(b"\r\n")
        if b"\r\n\r\n" not in chunk:
            continue
        header, body = chunk.split(b"\r\n\r\n", 1)
        if len(body) > 100000:
            raise SsiosError("too_large", "upload rejected", 413)
        found = re.search(br'name="([^"]+)"', header)
        if found is None:
            continue
        name = found.group(1).decode("ascii", "replace")
        if name not in {"csrf", "op", "service", "client_id", "client_secret", "client_file", "confirm", "redirect_url", "account_id"}:
            continue
        try:
            result[name] = body.decode("utf-8")
        except UnicodeError as exc:
            raise SsiosError("oauth_client_invalid", "the client file is not text", 400) from exc
    return result


class AppHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, plane) -> None:
        self.plane = plane
        super().__init__(address, AppHandler)


class AppHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def do_OPTIONS(self) -> None:
        self._send(403, {"error": "cors_denied", "detail": "browser origins are rejected"})

    def log_message(self, fmt: str, *args) -> None:
        return

    def _dispatch(self) -> None:
        try:
            if self.command not in ("GET", "POST"):
                raise SsiosError("method_not_allowed", "method rejected", 405)
            lan = lan_enabled(self.server.plane.settings)
            peer = self.client_address[0]
            require_peer(peer, lan=lan)
            require_host(self.headers.get("Host", ""), lan=lan)
            parts = urlsplit(self.path)
            if self.command == "GET" and parts.path == "/" and _accepts_html(self.headers):
                origin = self.headers.get("Origin") or ""
                if origin:
                    port = self.server.plane.bound_port or self.server.plane.settings.port
                    if not origin_allowed(origin, lan=lan, port=int(port)):
                        raise SsiosError("cors_denied", "browser origins are rejected", 403)
                self._redirect(_browser_home(self.server.plane, self.headers))
                return
            if (self.headers.get("Upgrade") or "").lower() == "websocket" and not (parts.path == "/ui" or parts.path.startswith("/ui/")):
                raise SsiosError("unauthorized", "authentication required", 401)
            if parts.path == "/ui" or parts.path.startswith("/ui/"):
                self._ui()
                return
            if lan and not is_loopback(peer):
                raise SsiosError("unauthorized", "authentication required", 401)
            if self.headers.get("Origin"):
                raise SsiosError("cors_denied", "browser origins are rejected", 403)
            token = self._bearer()
            status, payload = self._route(token)
            self._send(status, payload)
        except SsiosError as exc:
            self._send(exc.status, {"error": exc.code, "detail": exc.detail})
        except Exception as exc:
            from ssios.logsafe import INTERNAL_HINT, log_failure

            log_failure(exc)
            self._send(500, {"error": "internal", "detail": INTERNAL_HINT})

    def _route(self, token: str) -> tuple[int, dict]:
        plane = self.server.plane
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        query = {key: values[-1] for key, values in parse_qs(parts.query).items()}
        if self.command == "GET" and path == "/v1/health":
            return 200, plane.health(token)
        if self.command == "GET" and path == "/v1/status":
            return 200, plane.status(token)
        if self.command == "GET" and path == "/v1/runtime/preflight":
            return 200, plane.preflight(token)
        if self.command == "POST" and path == "/v1/tasks":
            return 202, plane.create_task(token, self._body())
        if self.command == "GET" and path.startswith("/v1/tasks/"):
            return 200, plane.get_task(token, path.split("/")[-1])
        if self.command == "POST" and path.startswith("/v1/tasks/") and path.endswith("/cancel"):
            task_id = path.split("/")[-2]
            return 200, plane.cancel_task(token, task_id)
        if self.command == "POST" and path.startswith("/v1/tasks/") and path.endswith("/reissue"):
            task_id = path.split("/")[-2]
            return 200, plane.reissue_expired(token, task_id)
        if self.command == "GET" and path.startswith("/v1/approvals/"):
            return 200, plane.get_approval(token, path.split("/")[-1])
        if self.command == "POST" and path.startswith("/v1/approvals/") and path.endswith("/decide"):
            approval_id = path.split("/")[-2]
            return 200, plane.decide(token, approval_id, self._body())
        if self.command == "GET" and path == "/v1/memory/core":
            return 200, plane.read_core(token, query.get("project_id", ""))
        if self.command == "GET" and path == "/v1/memory/file":
            return 200, plane.read_memory(token, query.get("project_id", ""), query.get("path", ""))
        if self.command == "GET" and path == "/v1/memory/search":
            return 200, plane.search_memory(token, query.get("project_id", ""), query.get("q", ""))
        if self.command == "POST" and path == "/v1/memory/propose":
            return 200, plane.propose_memory(token, self._body())
        if self.command == "POST" and path == "/v1/memory/forget":
            return 200, plane.forget_memory(token, self._body())
        if self.command == "GET" and path == "/v1/storage/input":
            return 200, plane.read_input(token, query.get("project_id", ""), query.get("revision", ""), query.get("path", ""))
        if self.command == "GET" and path == "/v1/storage/published":
            return 200, plane.read_published(token, query.get("project_id", ""), query.get("revision", "HEAD"))
        raise SsiosError("not_found", "route not found", 404)

    def _ui(self) -> None:
        try:
            body = self._form() if self.command == "POST" else {}
            page = handle_ui(self.server.plane, self.command, self.path, self.headers, body, peer_ip=self.client_address[0])
        except SsiosError as exc:
            if _ui_wants_html(self.headers):
                page = self._browser_failure(exc)
            else:
                raise
        except Exception as exc:
            if not _ui_wants_html(self.headers):
                raise
            from ssios.logsafe import INTERNAL_HINT, log_failure

            log_failure(exc)
            page = self._browser_failure(SsiosError("internal", INTERNAL_HINT, 500))
        if not isinstance(page, Page):
            from ssios.logsafe import INTERNAL_HINT

            raise SsiosError("internal", INTERNAL_HINT, 500)
        if page.stream is not None:
            self._stream(page)
            return
        data = page.body if isinstance(page.body, bytes) else page.body.encode("utf-8")
        self.send_response(page.status)
        if page.location:
            self.send_header("Location", page.location)
        self.send_header("Content-Type", page.content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", page.cache_control or "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'")
        self.send_header("Referrer-Policy", "same-origin")
        for item in getattr(page, "cookies", None) or ([page.cookie] if page.cookie else []):
            self.send_header("Set-Cookie", item)
        self.end_headers()
        self.wfile.write(data)

    def _browser_failure(self, exc: SsiosError) -> Page:
        from ssios.chat import public_error
        from ssios.ui import browser_error_page

        detail = public_error(exc)
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/ui/connections":
            try:
                from ssios.ui import _session
                from ssios.ui_household import _connections_page

                session = _session(self.server.plane, self.headers)
                if session is not None:
                    notice = f"<p class=\"notice\">{html.escape(detail)}</p>"
                    return Page(exc.status, _connections_page(self.server.plane, session, notice, self.client_address[0]))
            except Exception as nested:
                from ssios.logsafe import log_failure

                log_failure(nested)
        return browser_error_page(detail, exc.status)

    def _stream(self, page: Page) -> None:
        self.send_response(page.status)
        self.send_header("Content-Type", page.content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'")
        self.send_header("Referrer-Policy", "same-origin")
        for item in getattr(page, "cookies", None) or ([page.cookie] if page.cookie else []):
            self.send_header("Set-Cookie", item)
        self.end_headers()
        try:
            for chunk in page.stream:
                data = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
                self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        self.wfile.write(b"0\r\n\r\n")

    def _form(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise SsiosError("bad_request", "length rejected", 400) from exc
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        content_type = self.headers.get("Content-Type", "")
        if content_type.startswith("multipart/form-data"):
            return _multipart_memory(content_type, raw)
        if "application/json" in content_type:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise SsiosError("bad_request", "json object required", 400)
            return data
        parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}

    def _bearer(self) -> str:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise SsiosError("unauthorized", "authentication required", 401)
        token = header[7:].strip()
        if not token or len(token) > 512:
            raise SsiosError("unauthorized", "authentication required", 401)
        return token

    def _check_host(self) -> None:
        host = self.headers.get("Host", "").strip()
        if host.startswith("[") and "]" in host:
            name = host[1 : host.index("]")]
        elif host.count(":") == 1:
            name = host.split(":", 1)[0]
        else:
            name = host
        if name not in ("127.0.0.1", "localhost", "::1"):
            raise SsiosError("bad_host", "host rejected", 403)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise SsiosError("bad_request", "length rejected", 400) from exc
        if length < 0 or length > 262144:
            raise SsiosError("too_large", "body too large", 413)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SsiosError("bad_request", "json rejected", 400) from exc
        if not isinstance(data, dict):
            raise SsiosError("bad_request", "json object required", 400)
        return data

    def _redirect(self, target: str) -> None:
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)


def _ui_wants_html(headers) -> bool:
    """Browser page loads and form posts. Event streams and JSON clients stay JSON."""
    accept = str(headers.get("Accept") or "").lower()
    if "text/event-stream" in accept:
        return False
    if "application/json" in accept and "text/html" not in accept:
        return False
    if str(headers.get("X-Requested-With") or "").lower() == "xmlhttprequest" and "text/html" not in accept:
        return False
    if "text/html" in accept:
        return True
    return str(headers.get("Sec-Fetch-Mode") or "").lower() == "navigate"


def _accepts_html(headers) -> bool:
    return "text/html" in str(headers.get("Accept") or "").lower()


def _browser_home(plane, headers) -> str:
    from ssios.ui import _session

    if _session(plane, headers) is not None:
        return "/ui/chat"
    return "/ui/login"


class ControlServer:
    def __init__(self, plane) -> None:
        host = plane.settings.ui_bind or plane.settings.host
        if host == "localhost":
            host = "127.0.0.1"
        try:
            self.httpd = AppHTTPServer((host, plane.settings.port), plane)
        except OSError as exc:
            raise SsiosError(
                "port_unavailable",
                f"cannot bind {host}:{plane.settings.port} ({exc.errno})",
                500,
            ) from exc
        self.fingerprint = ""
        if lan_enabled(plane.settings):
            from ssios.tlsutil import ensure_certificate

            cert, key, digest = ensure_certificate(plane.settings.state_dir)
            self.fingerprint = digest
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(certfile=str(cert), keyfile=str(key))
            self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        plane.bound_port = self.httpd.server_address[1]
        self._thread = None
        self._stopped = False

    def start(self) -> None:
        import threading

        self._thread = threading.Thread(target=self.httpd.serve_forever, name="ssios-http", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._thread is not None:
            self.httpd.shutdown()
            self._thread.join(timeout=2)
        self.httpd.server_close()
