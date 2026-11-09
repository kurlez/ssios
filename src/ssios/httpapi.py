"""Localhost JSON API. Every route requires a bearer token."""

from __future__ import annotations

import json
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from ssios.errors import SsiosError


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
            self._check_host()
            if self.headers.get("Origin"):
                raise SsiosError("cors_denied", "browser origins are rejected", 403)
            token = self._bearer()
            status, payload = self._route(token)
            self._send(status, payload)
        except SsiosError as exc:
            self._send(exc.status, {"error": exc.code, "detail": exc.detail})
        except Exception:
            traceback.print_exc()
            self._send(500, {"error": "internal", "detail": "internal error"})

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

    def _bearer(self) -> str:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise SsiosError("unauthorized", "authentication required", 401)
        token = header[7:].strip()
        if not token or len(token) > 512:
            raise SsiosError("unauthorized", "authentication required", 401)
        return token

    def _check_host(self) -> None:
        host = self.headers.get("Host", "")
        name = host
        if host.count(":") == 1:
            name = host.split(":", 1)[0]
        if name not in ("127.0.0.1", "localhost"):
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

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)


class ControlServer:
    def __init__(self, plane) -> None:
        host = plane.settings.host
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
