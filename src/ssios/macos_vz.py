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
import shutil
import stat
import tarfile
import urllib.request
from pathlib import Path

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
    """Download pinned pieces on the host and pack the initramfs. Not used inside the guest."""
    pin = load_pin()
    cache = cache_dir(state_dir)
    cache.mkdir(parents=True, exist_ok=True)
    kernel = _cached(cache, pin["kernel"], timeout)
    alpine = _cached(cache, pin["alpine_minirootfs"], timeout)
    python_tar = _cached(cache, pin["python"], timeout)
    bwrap = _cached(cache, pin["bubblewrap"], timeout)
    libcap = _cached(cache, pin["libcap2"], timeout)
    root = cache / "root"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir()
    _extract_tar(alpine, root)
    python_root = root / "opt" / "python"
    python_root.mkdir(parents=True)
    _extract_tar(python_tar, python_root, strip_python_prefix=True)
    _extract_apk(bwrap, root)
    _extract_apk(libcap, root)
    init = root / "init"
    init.write_text(_INIT, encoding="utf-8")
    init.chmod(0o755)
    if not (python_root / "bin" / "python3").exists() and not (python_root / "bin" / "python3.12").exists():
        raise SsiosError("macos_vz_not_prepared", "pinned python has no python3 binary", 500)
    if not (python_root / "bin" / "python3").exists():
        (python_root / "bin" / "python3").symlink_to("python3.12")
    destination = cache / "codex-initrd.img"
    pack_newc(root, destination)
    return {
        "ok": True,
        "backend": "macos-vz",
        "vfkit": VFKIT_VERSION,
        "kernel": str(kernel),
        "initramfs": str(destination),
        "detail": "aarch64 kernel and initramfs are cached on the host. The guest was not booted.",
    }


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


def _extract_tar(archive: Path, destination: Path, strip_python_prefix: bool = False) -> None:
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                continue
            name = member.name[2:] if member.name.startswith("./") else member.name
            if name.startswith("/") or not name or ".." in Path(name).parts:
                raise SsiosError("macos_vz_not_prepared", f"archive member rejected: {name[:80]}", 500)
            if strip_python_prefix and (name == "python" or name.startswith("python/")):
                name = name[len("python/") :]
                if name in ("", "."):
                    continue
            member.name = name
            if member.issym() and (member.linkname or "").startswith("/"):
                _place_guest_symlink(name, member.linkname, destination)
                continue
            target = (destination / name).resolve()
            if destination.resolve() not in target.parents and target != destination.resolve():
                raise SsiosError("macos_vz_not_prepared", f"archive member rejected: {name[:80]}", 500)
            try:
                tar.extract(member, destination, filter="data")
            except tarfile.FilterError as exc:
                raise SsiosError("macos_vz_not_prepared", f"archive member rejected: {name[:80]}", 500) from exc


def _place_guest_symlink(name: str, linkname: str, destination: Path) -> None:
    """Alpine busybox applets point at /bin/busybox. Keep that path inside the guest root."""
    if "\x00" in linkname or ".." in Path(linkname).parts:
        raise SsiosError("macos_vz_not_prepared", f"archive member rejected: {name[:80]}", 500)
    parent = Path(name).parent
    guest_parent = "/" if str(parent) in ("", ".") else "/" + parent.as_posix()
    relative = os.path.relpath(linkname, guest_parent)
    if relative.startswith("../"):
        host_target = (destination / parent / relative).resolve()
        if destination.resolve() not in host_target.parents and host_target != destination.resolve():
            raise SsiosError("macos_vz_not_prepared", f"archive member rejected: {name[:80]}", 500)
    dest = destination / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink() or dest.exists():
        dest.unlink()
    dest.symlink_to(relative)


def _extract_apk(archive: Path, destination: Path) -> None:
    payload = gzip.decompress(archive.read_bytes())
    with tarfile.open(fileobj=io.BytesIO(payload)) as tar:
        for member in tar.getmembers():
            if not member.name.startswith("usr/") and not member.name.startswith("./usr/") and not member.name.startswith("lib/"):
                continue
            name = member.name[2:] if member.name.startswith("./") else member.name
            if ".." in Path(name).parts:
                raise SsiosError("macos_vz_not_prepared", f"apk member rejected: {name[:80]}", 500)
            member.name = name
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                continue
            if member.issym() and (member.linkname or "").startswith("/"):
                _place_guest_symlink(name, member.linkname, destination)
                continue
            try:
                tar.extract(member, destination, filter="data")
            except tarfile.FilterError as exc:
                raise SsiosError("macos_vz_not_prepared", f"apk member rejected: {name[:80]}", 500) from exc


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
