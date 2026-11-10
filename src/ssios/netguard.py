"""Source address, Host, and Origin checks for loopback and opt-in LAN."""

from __future__ import annotations

import ipaddress
import socket
import time
from urllib.parse import urlsplit

from ssios.errors import SsiosError

_RFC1918 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
_LINK_LOCAL_V4 = ipaddress.ip_network("169.254.0.0/16")
_ULA = ipaddress.ip_network("fc00::/7")
_LINK_LOCAL_V6 = ipaddress.ip_network("fe80::/10")


def parse_ip(value: str):
    text = (value or "").strip()
    if not text:
        return None
    if text.startswith("[") and "]" in text:
        text = text[1 : text.index("]")]
    if "%" in text:
        text = text.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def is_loopback(value: str) -> bool:
    addr = parse_ip(value)
    return bool(addr is not None and addr.is_loopback)


def is_private_source(value: str) -> bool:
    """RFC1918, IPv4 link-local, IPv6 ULA, or IPv6 link-local. Loopback is separate."""
    addr = parse_ip(value)
    if addr is None or addr.is_loopback or addr.is_multicast or addr.is_unspecified:
        return False
    if isinstance(addr, ipaddress.IPv4Address):
        return any(addr in network for network in _RFC1918) or addr in _LINK_LOCAL_V4
    return addr in _ULA or addr in _LINK_LOCAL_V6


def peer_allowed(value: str, *, lan: bool) -> bool:
    if is_loopback(value):
        return True
    return bool(lan and is_private_source(value))


def require_peer(value: str, *, lan: bool) -> None:
    if not peer_allowed(value, lan=lan):
        raise SsiosError("peer_rejected", "source address is not allowed", 403)


def machine_names() -> set[str]:
    host = socket.gethostname().split(".")[0].strip().lower()
    names = set()
    if host and host != "localhost":
        names.add(host)
        names.add(f"{host}.local")
    return names


def split_host_header(header: str) -> str:
    text = (header or "").strip()
    if text.startswith("[") and "]" in text:
        return text[1 : text.index("]")].lower()
    if text.count(":") == 1:
        return text.split(":", 1)[0].lower()
    return text.lower()


def host_allowed(header: str, *, lan: bool) -> bool:
    name = split_host_header(header)
    if name in {"127.0.0.1", "localhost", "::1"}:
        return True
    if not lan:
        return False
    if name in machine_names():
        return True
    return is_private_source(name)


def require_host(header: str, *, lan: bool) -> None:
    if not host_allowed(header, lan=lan):
        raise SsiosError("bad_host", "host rejected", 403)


def origin_allowed(value: str, *, lan: bool, port: int) -> bool:
    text = (value or "").strip()
    if not text or text.lower() == "null":
        return False
    parts = urlsplit(text if "://" in text else "")
    if parts.username or parts.password or parts.port is None:
        return False
    if lan:
        if parts.scheme != "https":
            return False
    elif parts.scheme != "http":
        return False
    if int(parts.port) != int(port):
        return False
    host = parts.hostname or ""
    if host == "::1":
        shown = "::1"
    else:
        shown = host.lower()
    return host_allowed(shown, lan=lan)


def local_urls(port: int, *, tls: bool) -> list[str]:
    scheme = "https" if tls else "http"
    urls = [f"{scheme}://127.0.0.1:{port}"]
    if not tls:
        return urls
    host = socket.gethostname().split(".")[0].strip() or "ssios"
    urls.append(f"{scheme}://{host}.local:{port}")
    seen = {urls[0], urls[1]}
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None)
    except OSError:
        infos = []
    for info in infos:
        ip = info[4][0]
        if not is_private_source(ip):
            continue
        url = f"{scheme}://{ip}:{port}"
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


class AttemptLimiter:
    """Fixed-window limiter for login and sign-up. Keys are source addresses."""

    def __init__(self, *, limit: int, window: float, lock_seconds: float) -> None:
        self.limit = limit
        self.window = window
        self.lock_seconds = lock_seconds
        self._hits: dict[str, list[float]] = {}
        self._locked: dict[str, float] = {}

    def check(self, key: str) -> None:
        now = time.monotonic()
        locked = self._locked.get(key, 0.0)
        if locked > now:
            raise SsiosError("rate_limited", "too many attempts; try again later", 429)
        hits = [item for item in self._hits.get(key, []) if now - item < self.window]
        self._hits[key] = hits
        if len(hits) >= self.limit:
            self._locked[key] = now + self.lock_seconds
            raise SsiosError("rate_limited", "too many attempts; try again later", 429)

    def fail(self, key: str) -> None:
        now = time.monotonic()
        hits = [item for item in self._hits.get(key, []) if now - item < self.window]
        hits.append(now)
        self._hits[key] = hits
        if len(hits) >= self.limit:
            self._locked[key] = now + self.lock_seconds

    def succeed(self, key: str) -> None:
        self._hits.pop(key, None)
        self._locked.pop(key, None)
