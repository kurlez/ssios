"""macOS Codex guest through vfkit, with no virtio-net device.

vfkit 0.6.4 (Apache-2.0) exposes Apple's Virtualization.framework. Every
device is a ``--device`` flag. This module never emits ``virtio-net``. The
guest reaches the host model proxy by connecting AF_VSOCK port 19090, which
vfkit presents as a Unix socket the host listens on.

The aarch64 kernel and root pieces are downloaded only by ``prepare``, on the
Mac, and checked against third_party/macos-vz.pin.json. A task boot does not
download anything. Linux CI cannot start Virtualization.framework.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import posixpath
import shutil
import stat
import tarfile
import urllib.request
from pathlib import Path, PurePosixPath

from ssios.codex_guest import VSOCK_PORT
from ssios.errors import SsiosError

PIN_PATH = Path(__file__).resolve().parents[2] / "third_party" / "macos-vz.pin.json"
VFKIT_VERSION = "0.6.4"

_INIT = """#!/bin/busybox sh
/bin/busybox mount -t proc proc /proc
/bin/busybox mount -t sysfs sysfs /sys
/bin/busybox mount -t devtmpfs devtmpfs /dev
/bin/busybox mkdir -p /input /scratch /workspace /tools /tmp /opt
/bin/busybox mount -t virtiofs input /input
/bin/busybox mount -t virtiofs scratch /scratch
/bin/busybox mount -t virtiofs workspace /workspace
/bin/busybox mount -t virtiofs tools /tools
/bin/busybox cat > /bin/bash <<'EOF'
#!/bin/busybox sh
exec /bin/busybox sh "$@"
EOF
/bin/busybox chmod 755 /bin/bash
/bin/busybox ip link set lo up
/bin/busybox ip addr add 127.0.0.1/8 dev lo
export PATH=/opt/python/bin:/usr/bin:/bin:/tools
export LD_LIBRARY_PATH=/opt/python/lib:/usr/lib:/lib
/opt/python/bin/python3 /scratch/entry.py
/bin/busybox poweroff -f
"""


def load_pin() -> dict:
    try:
        pin = json.loads(PIN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SsiosError("macos_vz_pin_invalid", "macos-vz pin file cannot be read", 500) from exc
    if pin.get("vfkit", {}).get("version") != VFKIT_VERSION:
        raise SsiosError("macos_vz_pin_invalid", "macos-vz pin is not vfkit 0.6.4", 500)
    return pin


def assert_no_nic(argv: list[str]) -> None:
    for part in argv:
        lowered = part.casefold()
        if "virtio-net" in lowered or lowered.startswith("net,") or ",nat" in lowered or "unixsocketpath" in lowered:
            raise SsiosError("network_denied", "macos-vz refuses a guest NIC", 403)


def vfkit_command(
    *,
    binary: str,
    kernel: Path,
    initrd: Path,
    serial: Path,
    vsock: Path,
    shares: list[tuple[str, Path]],
    cpus: int,
    memory_mb: int,
) -> list[str]:
    if cpus < 1 or memory_mb < 128:
        raise SsiosError("runtime_invalid", "microvm size rejected", 400)
    for path in (kernel, initrd, serial, vsock, *(item for _tag, item in shares)):
        _reject_path(path)
    cmdline = "console=hvc0 reboot=k panic=1 rdinit=/init"
    argv = [
        binary,
        "--cpus",
        str(cpus),
        "--memory",
        str(memory_mb),
        "--bootloader",
        f"linux,kernel={kernel},initrd={initrd},cmdline={cmdline}",
        "--device",
        f"virtio-serial,logFilePath={serial}",
        "--device",
        f"virtio-vsock,port={VSOCK_PORT},socketURL={vsock},listen",
    ]
    for tag, path in shares:
        if tag not in {"input", "scratch", "workspace", "tools"}:
            raise SsiosError("runtime_invalid", "virtio-fs tag rejected", 400)
        argv.extend(["--device", f"virtio-fs,sharedDir={path},mountTag={tag}"])
    assert_no_nic(argv)
    return argv


def cache_dir(state_dir: Path) -> Path:
    return state_dir / "cache" / "macos-vz"


def prepared_kernel(state_dir: Path) -> Path:
    path = cache_dir(state_dir) / "Image-arm64"
    if not path.is_file() or path.is_symlink():
        raise SsiosError("macos_vz_not_prepared", "aarch64 kernel is missing; run the macos-vz prepare step", 503)
    verify_sha256(path, load_pin()["kernel"]["sha256"])
    return path


def prepared_initramfs(state_dir: Path) -> Path:
    path = cache_dir(state_dir) / "codex-initrd.img"
    if not path.is_file() or path.is_symlink():
        raise SsiosError("macos_vz_not_prepared", "aarch64 initramfs is missing; run the macos-vz prepare step", 503)
    return path


def prepare_macos_vz(state_dir: Path, timeout: float = 600) -> dict:
    """Download pinned pieces on the host and pack the initramfs. Not used inside the guest.

    The guest tree is never extracted onto the host filesystem. Case-insensitive
    APFS folds terminfo names such as ``N/`` and ``n/`` together, and Python's
    tar filter then loops in ``realpath``. The cpio is built from tar members in
    memory, so host case folding cannot apply.
    """
    pin = load_pin()
    cache = cache_dir(state_dir)
    cache.mkdir(parents=True, exist_ok=True)
    kernel = _cached(cache, pin["kernel"], timeout)
    alpine = _cached(cache, pin["alpine_minirootfs"], timeout)
    python_tar = _cached(cache, pin["python"], timeout)
    bwrap = _cached(cache, pin["bubblewrap"], timeout)
    libcap = _cached(cache, pin["libcap2"], timeout)
    leftover = cache / "root"
    if leftover.exists():
        shutil.rmtree(leftover)
    image = GuestImage()
    collisions = {
        "alpine": len(case_collisions(archive_names(alpine))),
        "python": len(case_collisions(archive_names(python_tar))),
        "bubblewrap": len(case_collisions(archive_names(bwrap))),
        "libcap2": len(case_collisions(archive_names(libcap))),
    }
    add_tar(image, alpine)
    add_tar(image, python_tar, dest_prefix="opt/python", strip_prefix="python/", skip=_PYTHON_SKIP)
    add_apk(image, bwrap)
    add_apk(image, libcap)
    if "opt/python/bin/python3" not in image.names and "opt/python/bin/python3.12" in image.names:
        image.add_link("opt/python/bin/python3", "python3.12")
    if "opt/python/bin/python3" not in image.names:
        raise SsiosError("macos_vz_not_prepared", "pinned python has no python3 binary", 500)
    image.add_file("init", _INIT.encode(), 0o755, replace=True)
    destination = cache / "codex-initrd.img"
    image.write_gzip(destination)
    return {
        "ok": True,
        "backend": "macos-vz",
        "vfkit": VFKIT_VERSION,
        "kernel": str(kernel),
        "initramfs": str(destination),
        "case_collisions": collisions,
        "detail": (
            "aarch64 initramfs was packed from archive members without extracting onto the host. "
            f"Case-only name pairs: alpine {collisions['alpine']}, python {collisions['python']} "
            "(python share/terminfo is omitted), "
            f"bubblewrap {collisions['bubblewrap']}, libcap2 {collisions['libcap2']}. "
            "The guest was not booted."
        ),
    }


_PYTHON_SKIP = (
    "share",
    "include",
    "lib/python3.12/test",
    "lib/python3.12/idlelib",
    "lib/python3.12/tkinter",
    "lib/python3.12/turtledemo",
    "lib/python3.12/ensurepip",
)


def case_collisions(names: list[str]) -> list[tuple[str, str]]:
    """Pairs whose paths differ only by case. The guest keeps both; the host must not."""
    seen: dict[str, str] = {}
    found: list[tuple[str, str]] = []
    for name in names:
        key = name.casefold()
        previous = seen.get(key)
        if previous is None:
            seen[key] = name
        elif previous != name:
            found.append((previous, name))
    return found


class GuestImage:
    """Cpio members addressed by guest path. Nothing is created on the host filesystem."""

    def __init__(self) -> None:
        self.items: list[tuple[str, int, bytes]] = []
        self.names: set[str] = set()
        self._files: dict[str, bytes] = {}

    def add_file(self, name: str, data: bytes, mode: int = 0o644, *, replace: bool = False) -> None:
        self._put(name, stat.S_IFREG | (mode & 0o777), data, replace=replace)
        self._files[self._clean(name)] = data

    def add_dir(self, name: str, mode: int = 0o755) -> None:
        self._put(name, stat.S_IFDIR | (mode & 0o777), b"")

    def add_link(self, name: str, target: str, *, replace: bool = False) -> None:
        relative = _link_target(name, target)
        self._put(name, stat.S_IFLNK | 0o777, relative.encode(), replace=replace)

    def cpio(self) -> bytes:
        blob = bytearray()
        ordered = [(".", stat.S_IFDIR | 0o755, b"")] + self.items
        for index, (name, mode, body) in enumerate(ordered, start=1):
            blob += _newc_header(name, mode, len(body), index)
            blob += body
            blob += b"\0" * ((4 - (len(body) % 4)) % 4)
        blob += _newc_header("TRAILER!!!", 0, 0, len(ordered) + 1)
        return bytes(blob)

    def write_gzip(self, destination: Path) -> None:
        temporary = destination.with_suffix(destination.suffix + ".partial")
        temporary.write_bytes(gzip.compress(self.cpio(), mtime=0))
        os.replace(temporary, destination)

    def _put(self, name: str, mode: int, body: bytes, *, replace: bool = False) -> None:
        cleaned = self._clean(name)
        self._ensure_parents(cleaned)
        if cleaned in self.names:
            if not replace:
                return
            self.items = [(item_name, item_mode, item_body) for item_name, item_mode, item_body in self.items if item_name != cleaned]
            self.names.discard(cleaned)
        self.items.append((cleaned, mode, body))
        self.names.add(cleaned)

    def _ensure_parents(self, name: str) -> None:
        parent = PurePosixPath(name).parent.as_posix()
        if parent in ("", "."):
            return
        parts = PurePosixPath(parent).parts
        for index in range(1, len(parts) + 1):
            directory = "/".join(parts[:index])
            if directory not in self.names:
                self.items.append((directory, stat.S_IFDIR | 0o755, b""))
                self.names.add(directory)

    @staticmethod
    def _clean(name: str) -> str:
        return _clean_member_name(name)


def archive_names(path: Path) -> list[str]:
    names: list[str] = []
    for member, _payload in _open_archive(path):
        names.append(member.name)
    return names


def add_tar(
    image: GuestImage,
    path: Path,
    *,
    dest_prefix: str = "",
    strip_prefix: str = "",
    skip: tuple[str, ...] = (),
) -> None:
    for member, payload in _open_archive(path):
        _add_member(image, member, payload, dest_prefix=dest_prefix, strip_prefix=strip_prefix, skip=skip)


def add_apk(image: GuestImage, path: Path) -> None:
    for member, payload in _open_archive(path):
        name = _display_name(member.name)
        if not (name == "usr" or name.startswith("usr/") or name == "lib" or name.startswith("lib/")):
            continue
        _add_member(image, member, payload)


def cpio_entries(blob: bytes) -> dict[str, bytes]:
    entries: dict[str, bytes] = {}
    position = 0
    while position + 110 <= len(blob):
        if blob[position : position + 6] != b"070701":
            break
        namesize = int(blob[position + 94 : position + 102], 16)
        filesize = int(blob[position + 54 : position + 62], 16)
        name_at = position + 110
        name = blob[name_at : name_at + namesize].split(b"\0", 1)[0].decode()
        data_at = name_at + namesize
        data_at += (4 - (data_at % 4)) % 4
        body = blob[data_at : data_at + filesize]
        position = data_at + filesize
        position += (4 - (position % 4)) % 4
        if name == "TRAILER!!!":
            break
        entries[name] = body
    return entries


def pack_tar_bytes(blob: bytes, *, dest_prefix: str = "", strip_prefix: str = "", skip: tuple[str, ...] = ()) -> bytes:
    """Pack one tar from memory. Used by tests so nothing is written to the host."""
    image = GuestImage()
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        for member in tar.getmembers():
            payload = tar.extractfile(member) if member.isreg() else None
            data = b"" if payload is None else payload.read()
            _add_member(image, member, data, dest_prefix=dest_prefix, strip_prefix=strip_prefix, skip=skip)
    return image.cpio()


def _add_member(
    image: GuestImage,
    member: tarfile.TarInfo,
    payload: bytes,
    *,
    dest_prefix: str = "",
    strip_prefix: str = "",
    skip: tuple[str, ...] = (),
) -> None:
    if member.isdev() or member.isfifo() or member.ischr() or member.isblk():
        return
    if not (member.isdir() or member.isreg() or member.issym() or member.islnk()):
        return
    name = _display_name(member.name)
    if name in ("", "."):
        return
    if strip_prefix:
        if name == strip_prefix.rstrip("/"):
            return
        prefix = strip_prefix if strip_prefix.endswith("/") else strip_prefix + "/"
        if not name.startswith(prefix):
            raise SsiosError("macos_vz_not_prepared", f"archive member rejected: {name[:80]}", 500)
        name = name[len(prefix) :]
    if _skipped(name, skip):
        return
    if dest_prefix:
        name = dest_prefix.rstrip("/") + "/" + name
    if member.isdir():
        image.add_dir(name, member.mode or 0o755)
        return
    if member.issym():
        image.add_link(name, member.linkname or "")
        return
    if member.islnk():
        target = _display_name(member.linkname or "")
        if strip_prefix and target.startswith(strip_prefix if strip_prefix.endswith("/") else strip_prefix + "/"):
            target = target[len(strip_prefix if strip_prefix.endswith("/") else strip_prefix + "/") :]
        if dest_prefix:
            target = dest_prefix.rstrip("/") + "/" + target
        body = image._files.get(_clean_member_name(target))
        if body is None:
            raise SsiosError("macos_vz_not_prepared", f"hard link target is missing: {name[:80]}", 500)
        image.add_file(name, body, member.mode or 0o644)
        return
    image.add_file(name, payload, member.mode or 0o644)


def _skipped(name: str, skip: tuple[str, ...]) -> bool:
    parts = PurePosixPath(name).parts
    if "__pycache__" in parts:
        return True
    for prefix in skip:
        if name == prefix or name.startswith(prefix + "/"):
            return True
    return False


def _display_name(name: str) -> str:
    if name.startswith("./"):
        name = name[2:]
    return name


def _clean_member_name(name: str) -> str:
    if "\x00" in name or "\\" in name or name.startswith("/"):
        raise SsiosError("macos_vz_not_prepared", f"archive member rejected: {name[:80]}", 500)
    if name.startswith("./"):
        name = name[2:]
    parts = PurePosixPath(name).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise SsiosError("macos_vz_not_prepared", f"archive member rejected: {name[:80]}", 500)
    return "/".join(parts)


def _link_target(name: str, linkname: str) -> str:
    cleaned = _clean_member_name(name)
    if not linkname or "\x00" in linkname or "\\" in linkname:
        raise SsiosError("macos_vz_not_prepared", f"link rejected: {cleaned[:80]}", 500)
    if linkname.startswith("/"):
        if ".." in PurePosixPath(linkname).parts:
            raise SsiosError("macos_vz_not_prepared", f"link rejected: {cleaned[:80]}", 500)
        parent = PurePosixPath(cleaned).parent.as_posix()
        base = "/" if parent == "." else "/" + parent
        relative = posixpath.relpath(linkname, base)
    else:
        relative = linkname
    parent = PurePosixPath(cleaned).parent.as_posix()
    base = "/" if parent == "." else "/" + parent
    landed = posixpath.normpath(posixpath.join(base, relative))
    if not landed.startswith("/") or ".." in PurePosixPath(landed).parts:
        raise SsiosError("macos_vz_not_prepared", f"link rejected: {cleaned[:80]}", 500)
    return relative


def _open_archive(path: Path):
    if path.name.endswith(".apk"):
        payload = gzip.decompress(path.read_bytes())
        tar = tarfile.open(fileobj=io.BytesIO(payload))
    else:
        tar = tarfile.open(path)
    try:
        for member in tar.getmembers():
            data = b""
            if member.isreg():
                source = tar.extractfile(member)
                data = b"" if source is None else source.read()
            yield member, data
    finally:
        tar.close()


def pack_newc(root: Path, destination: Path) -> None:
    blob = bytearray()
    entries = [root]
    for path in sorted(root.rglob("*")):
        entries.append(path)
    for index, path in enumerate(entries):
        name = "." if path == root else path.relative_to(root).as_posix()
        info = path.lstat()
        if path.is_symlink():
            target = os.readlink(path).encode()
            mode = stat.S_IFLNK | 0o777
            body = target
        elif path.is_dir():
            mode = stat.S_IFDIR | (info.st_mode & 0o777)
            body = b""
        elif path.is_file():
            mode = stat.S_IFREG | (info.st_mode & 0o777)
            body = path.read_bytes()
        else:
            continue
        blob += _newc_header(name, mode, len(body), index + 1)
        blob += body
        blob += b"\0" * ((4 - (len(body) % 4)) % 4)
    blob += _newc_header("TRAILER!!!", 0, 0, len(entries) + 1)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(gzip.compress(bytes(blob), mtime=0))


def verify_sha256(path: Path, expected: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise SsiosError("macos_vz_checksum_mismatch", "macos-vz file checksum does not match the pin", 500)


def _cached(cache: Path, spec: dict, timeout: float) -> Path:
    path = cache / spec["name"]
    if not path.is_file():
        _download(spec["url"], path, timeout)
    try:
        verify_sha256(path, spec["sha256"])
    except SsiosError:
        path.unlink(missing_ok=True)
        raise
    return path


def _download(url: str, destination: Path, timeout: float) -> None:
    temporary = destination.with_suffix(destination.suffix + ".partial")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response, temporary.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise SsiosError("macos_vz_not_prepared", "could not download a pinned macos-vz file", 503) from exc
    os.replace(temporary, destination)


def _newc_header(name: str, mode: int, size: int, inode: int) -> bytes:
    encoded = name.encode() + b"\0"
    if len(encoded) > 0xFFFFFFFF or size > 0xFFFFFFFF or inode > 0xFFFFFFFF:
        raise SsiosError("macos_vz_not_prepared", "initramfs member does not fit the newc header", 500)
    fields = [
        "070701",
        f"{inode:08x}",
        f"{mode:08x}",
        "00000000",
        "00000000",
        "00000001",
        "00000000",
        f"{size:08x}",
        "00000000",
        "00000000",
        "00000000",
        "00000000",
        f"{len(encoded):08x}",
        "00000000",
    ]
    header = "".join(fields).encode()
    pad = (4 - ((len(header) + len(encoded)) % 4)) % 4
    return header + encoded + (b"\0" * pad)


def boot_vfkit(runtime, session, text: str, cancel, deadline: float, kernel: Path, initramfs: Path, upstream: str | None):
    del cancel
    import time

    from ssios.codex_guest import RequestBudget, ResponsesProxy, _failure_detail, _snapshot, workspace_diff
    from ssios.vmguest import GuestOutcome, _command_record, _ms, _read_json, _run

    started = time.monotonic()
    budget = RequestBudget(runtime.settings.codex_max_requests, runtime.settings.codex_max_request_bytes)
    vsock = session.root / "vfkit.vsock"
    proxy = ResponsesProxy(
        session.root / "vsock.sock",
        text,
        runtime.settings.codex_model,
        budget,
        session.root / "proxy.log",
        upstream=upstream,
        allow_models=tuple(getattr(runtime.settings, "codex_allow_models", (runtime.settings.codex_model,))),
        max_output_tokens=runtime.settings.max_output_tokens,
        listen_path=vsock,
    )
    before = _snapshot(session.root / "workspace")
    records: list[dict] = []
    binary = runtime.binary()
    if not binary:
        return GuestOutcome("", False, runtime.name, None, "vfkit is not installed", records)
    try:
        proxy.start()
        command = vfkit_command(
            binary=binary,
            kernel=kernel,
            initrd=initramfs,
            serial=session.root / "serial.log",
            vsock=vsock,
            shares=[
                ("input", session.root / "input"),
                ("scratch", session.root / "scratch"),
                ("workspace", session.root / "workspace"),
                ("tools", session.root / "tools"),
            ],
            cpus=runtime.settings.cpus,
            memory_mb=runtime.settings.memory_mb,
        )
        (session.root / "hypervisor.argv").write_text("\n".join(command) + "\n", encoding="utf-8")
        completed = _run(command, max(1.0, deadline - time.monotonic()))
        records.append(_command_record(completed))
        output = _read_json(session.root / "scratch" / "output.json")
        net = _read_json(session.root / "scratch" / "netcheck.json")
        (session.root / "patch.diff").write_text(workspace_diff(before, session.root / "workspace"), encoding="utf-8")
        text_out = output.get("text", "") if isinstance(output, dict) else ""
        net_ok = isinstance(net, dict) and net.get("interfaces") == ["lo"] and net.get("outbound") is False
        ran = (
            completed.returncode == 0
            and isinstance(output, dict)
            and output.get("exit_code") == 0
            and isinstance(text_out, str)
            and bool(text_out.strip())
            and net_ok
            and proxy.served >= 1
            and not proxy.rejected
        )
        detail = "guest wrote output.json" if ran else _failure_detail(session, completed, net, proxy)
        return GuestOutcome(text_out if ran else "", ran, runtime.name, _ms(started), detail, records)
    finally:
        proxy.close()


def _reject_path(path: Path) -> None:
    text = str(path)
    if not text.startswith("/") or "," in text or " " in text or "\n" in text or ".." in path.parts:
        raise SsiosError("runtime_invalid", "vfkit path rejected", 400)
