"""Build the tiny Linux initramfs used by Cloud Hypervisor on this host.

The Mac path does not use these files. krunvm boots a pinned OCI image instead.
"""

from __future__ import annotations

import gzip
import os
import shutil
import subprocess
from pathlib import Path

from ssios.errors import SsiosError


def build_echo_initramfs(destination: Path) -> None:
    root = destination.parent / (destination.name + ".root")
    if root.exists():
        shutil.rmtree(root)
    _busybox_root(root)
    _write_init(
        root,
        "#!/bin/busybox sh\n"
        "/bin/busybox mount -t proc proc /proc\n"
        "/bin/busybox mount -t sysfs sysfs /sys\n"
        "/bin/busybox mount -t devtmpfs devtmpfs /dev\n"
        "echo ok\n"
        "/bin/busybox poweroff -f\n",
    )
    _pack(root, destination)
    shutil.rmtree(root)


def build_codex_initramfs(destination: Path) -> None:
    """Initramfs that only mounts the task shares and runs /scratch/entry.py.

    The Codex binary is not inside this image. The host places the checked
    binary on the tools share before boot.
    """
    root = destination.parent / (destination.name + ".root")
    if root.exists():
        shutil.rmtree(root)
    _busybox_root(root)
    _copy_python(root)
    bwrap = Path("/usr/bin/bwrap")
    if bwrap.is_file():
        _copy_elf(bwrap, root)
    _write_init(
        root,
        "#!/bin/busybox sh\n"
        "/bin/busybox mount -t proc proc /proc\n"
        "/bin/busybox mount -t sysfs sysfs /sys\n"
        "/bin/busybox mount -t devtmpfs devtmpfs /dev\n"
        "/bin/busybox mkdir -p /input /scratch /workspace /tools /opt\n"
        "/bin/busybox mount -t virtiofs input /input\n"
        "/bin/busybox mount -t virtiofs scratch /scratch\n"
        "/bin/busybox mount -t virtiofs workspace /workspace\n"
        "/bin/busybox mount -t virtiofs tools /tools\n"
        "/bin/busybox ln -sf busybox /bin/sh\n"
        "/bin/busybox cat > /bin/bash <<'EOF'\n"
        "#!/bin/busybox sh\n"
        "exec /bin/busybox sh \"$@\"\n"
        "EOF\n"
        "/bin/busybox chmod 755 /bin/bash\n"
        "/bin/busybox mkdir -p /tmp /opt /etc\n"
        "/bin/busybox ip link set lo up\n"
        "/bin/busybox ip addr add 127.0.0.1/8 dev lo\n"
        "export LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu:/lib/x86_64-linux-gnu\n"
        "/usr/bin/python3 /scratch/entry.py\n"
        "/bin/busybox poweroff -f\n",
    )
    _pack(root, destination)
    shutil.rmtree(root)


def build_worker_initramfs(destination: Path) -> None:
    root = destination.parent / (destination.name + ".root")
    if root.exists():
        shutil.rmtree(root)
    _busybox_root(root)
    _copy_python(root)
    _write_init(
        root,
        "#!/bin/busybox sh\n"
        "/bin/busybox mount -t proc proc /proc\n"
        "/bin/busybox mount -t sysfs sysfs /sys\n"
        "/bin/busybox mount -t devtmpfs devtmpfs /dev\n"
        "/bin/busybox mkdir -p /input /scratch\n"
        "/bin/busybox mount -t virtiofs input /input\n"
        "/bin/busybox mount -t virtiofs scratch /scratch\n"
        "export LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu:/lib/x86_64-linux-gnu\n"
        "/usr/bin/python3 /scratch/worker.py\n"
        "/bin/busybox poweroff -f\n",
    )
    _pack(root, destination)
    shutil.rmtree(root)


def _busybox_root(root: Path) -> None:
    busybox = shutil.which("busybox")
    if not busybox:
        raise SsiosError("guest_image_missing", "busybox is not installed", 500)
    for folder in ("bin", "proc", "sys", "dev"):
        (root / folder).mkdir(parents=True)
    shutil.copy2(busybox, root / "bin" / "busybox")


def _write_init(root: Path, text: str) -> None:
    path = root / "init"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _copy_python(root: Path) -> None:
    binary = Path("/usr/bin/python3.12")
    if not binary.is_file():
        raise SsiosError("guest_image_missing", "host python3.12 is missing", 500)
    _copy_elf(binary, root)
    link = root / "usr" / "bin" / "python3"
    link.parent.mkdir(parents=True, exist_ok=True)
    if not link.exists():
        link.symlink_to("python3.12")
    stdlib = Path("/usr/lib/python3.12")
    skip = {"test", "tests", "__pycache__", "ensurepip", "idlelib", "turtledemo", "tkinter"}
    for path in stdlib.rglob("*"):
        if any(part in skip for part in path.parts):
            continue
        target = root / "usr" / "lib" / "python3.12" / path.relative_to(stdlib)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file() and not path.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            if path.suffix == ".so":
                _copy_elf(path, root)


def _copy_elf(path: Path, root: Path) -> None:
    seen = set()

    def place(item: Path) -> None:
        item = Path(item)
        if not item.exists():
            return
        key = str(item)
        if key in seen:
            return
        seen.add(key)
        destination = root / item.as_posix().lstrip("/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if item.is_symlink():
            # Copy the real file onto the path the ELF header names. A relative
            # symlink such as ../lib/... does not resolve inside the initramfs.
            real = item.resolve()
            if real.is_file() and not destination.exists():
                shutil.copy2(real, destination)
            place(real)
            return
        if item.is_file() and not destination.exists():
            shutil.copy2(item, destination)
        if not os.access(item, os.X_OK) and item.suffix != ".so" and "ld-linux" not in item.name:
            return
        completed = subprocess.run(["ldd", str(item)], check=False, capture_output=True, text=True)
        for line in completed.stdout.splitlines():
            parts = line.strip().split()
            if "=>" in parts and len(parts) >= 3 and parts[2].startswith("/"):
                place(Path(parts[2]))
            elif parts and parts[0].startswith("/") and "ld-linux" in parts[0]:
                place(Path(parts[0]))

    place(path)


def _pack(root: Path, destination: Path) -> None:
    if shutil.which("cpio") is None:
        raise SsiosError("guest_image_missing", "cpio is not installed", 500)
    listed = subprocess.run(["find", ".", "-print"], cwd=root, check=True, capture_output=True)
    packed = subprocess.run(["cpio", "-o", "-H", "newc"], cwd=root, input=listed.stdout, check=True, capture_output=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(gzip.compress(packed.stdout, mtime=0))
