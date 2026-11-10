"""vfkit argv stays without a NIC. The guest image packer does not need a Mac."""

from __future__ import annotations

import gzip
import io
import os
import stat
import tarfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ssios.codex_guest import UNIX_SOCKET_MAX, RequestBudget, ResponsesProxy, assert_unix_socket_path
from ssios.errors import SsiosError
from ssios.macos_vz import (
    allocate_proxy_socket,
    assert_no_nic,
    cpio_entries,
    pack_newc,
    pack_tar_bytes,
    release_proxy_socket,
    supervise_process,
    vfkit_command,
)


class ArgvTest(unittest.TestCase):
    def test_command_has_vsock_and_no_nic(self) -> None:
        argv = vfkit_command(
            binary="vfkit",
            kernel=Path("/tmp/Image-arm64"),
            initrd=Path("/tmp/codex-initrd.img"),
            serial=Path("/tmp/serial.log"),
            vsock=Path("/tmp/vfkit.vsock"),
            shares=[
                ("input", Path("/tmp/input")),
                ("scratch", Path("/tmp/scratch")),
                ("workspace", Path("/tmp/workspace")),
                ("tools", Path("/tmp/tools")),
            ],
            cpus=2,
            memory_mb=2048,
        )
        text = "\n".join(argv)
        self.assertIn("virtio-vsock,port=19090,socketURL=/tmp/vfkit.vsock,listen", text)
        self.assertIn("mountTag=workspace", text)
        self.assertIn("console=hvc0", text)
        self.assertNotIn("virtio-net", text)
        assert_no_nic(argv)
        with self.assertRaises(SsiosError) as caught:
            assert_no_nic(argv + ["--device", "virtio-net,nat"])
        self.assertEqual(caught.exception.code, "network_denied")

    def test_comma_in_a_path_is_rejected(self) -> None:
        with self.assertRaises(SsiosError):
            vfkit_command(
                binary="vfkit",
                kernel=Path("/tmp/Image,arm64"),
                initrd=Path("/tmp/initrd"),
                serial=Path("/tmp/serial.log"),
                vsock=Path("/tmp/vsock"),
                shares=[("input", Path("/tmp/input"))],
                cpus=1,
                memory_mb=1536,
            )


class SocketPathTest(unittest.TestCase):
    def test_deep_state_dir_still_gets_a_short_socket(self) -> None:
        deep = Path("/var/folders/qq/0123456789abcdef0123456789abcdef/T/ssios-vz-codex.Qx8f72/storage/tasks")
        deep = deep / ("task-" + "ab" * 8) / "guest"
        self.assertGreater(len(str(deep / "vfkit.vsock")), 104)
        directory, path = allocate_proxy_socket(deep)
        self.addCleanup(lambda: release_proxy_socket(directory))
        self.assertTrue(str(path).startswith("/tmp/ssios-"))
        self.assertLessEqual(len(os.fsencode(path)), UNIX_SOCKET_MAX)
        self.assertNotIn("var", path.parts)
        info = os.lstat(directory)
        self.assertFalse(stat.S_ISLNK(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o700)
        self.assertEqual(info.st_uid, os.getuid())
        log = Path("/tmp/ssios-socket-log")
        proxy = ResponsesProxy(path, "local", "mock-summary", RequestBudget(1, 1000), log, listen_path=path)
        proxy.start()
        try:
            bound = os.lstat(path)
            self.assertFalse(stat.S_ISLNK(bound.st_mode))
            self.assertEqual(stat.S_IMODE(bound.st_mode), 0o600)
            self.assertEqual(bound.st_uid, os.getuid())
        finally:
            proxy.close()
        release_proxy_socket(directory)
        self.assertFalse(directory.exists())

    def test_socket_length_guard(self) -> None:
        with self.assertRaises(SsiosError) as caught:
            assert_unix_socket_path(Path("/tmp") / ("n" * 120) / "vsock")
        self.assertEqual(caught.exception.code, "socket_path_too_long")


class ConnectGraceTest(unittest.TestCase):
    def test_no_connection_stops_the_process(self) -> None:
        class Proxy:
            connected = 0

        started = time.monotonic()
        completed, timed_out = supervise_process(["sleep", "30"], Proxy(), started + 5, connect_grace=0.3)
        self.assertTrue(timed_out)
        self.assertLess(time.monotonic() - started, 3)
        self.assertNotEqual(completed.returncode, 0)

    def test_upstream_4xx_stops_the_process(self) -> None:
        class Proxy:
            connected = 1
            upstream_failure = {"fatal": True, "error": "upstream_error", "status": 400}

        started = time.monotonic()
        completed, timed_out = supervise_process(["sleep", "30"], Proxy(), started + 5, connect_grace=30)
        self.assertFalse(timed_out)
        self.assertLess(time.monotonic() - started, 2)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(b"upstream 4xx", completed.stderr)


class PackTest(unittest.TestCase):
    def test_initramfs_contains_the_init_script(self) -> None:
        root = Path("/tmp/ssios-vz-pack")
        if root.exists():
            import shutil

            shutil.rmtree(root)
        (root / "bin").mkdir(parents=True)
        (root / "bin" / "busybox").write_bytes(b"busybox")
        link = root / "bin" / "sh"
        link.symlink_to("busybox")
        destination = root.parent / "ssios-vz-pack.img"
        pack_newc(root, destination)
        blob = gzip.decompress(destination.read_bytes())
        self.assertIn(b"bin/busybox", blob)
        self.assertIn(b"bin/sh", blob)
        self.assertTrue(blob.startswith(b"070701") or b"070701" in blob[:16])


def _tar_bytes(members: list[tuple[str, str, bytes | None]]) -> bytes:
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w") as tar:
        for name, kind, payload in members:
            info = tarfile.TarInfo(name)
            if kind == "file":
                data = payload or b""
                info.size = len(data)
                info.mode = 0o755
                tar.addfile(info, io.BytesIO(data))
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = payload.decode() if isinstance(payload, bytes) else str(payload)
                tar.addfile(info)
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif kind == "char":
                info.type = tarfile.CHRTYPE
                tar.addfile(info)
            else:
                raise AssertionError(kind)
    return blob.getvalue()


class ArchivePackTest(unittest.TestCase):
    def test_case_collision_never_touches_the_host_filesystem(self) -> None:
        blob = _tar_bytes(
            [
                ("share/terminfo/n/ncr260vt300wpp", "file", b"term"),
                ("share/terminfo/N/NCR260VT300WPP", "symlink", b"../n/ncr260vt300wpp"),
            ]
        )
        with mock.patch("os.symlink", side_effect=AssertionError("symlink")), mock.patch(
            "os.path.realpath", side_effect=AssertionError("realpath")
        ), mock.patch("pathlib.Path.resolve", side_effect=AssertionError("resolve")):
            payload = pack_tar_bytes(blob)
        entries = cpio_entries(payload)
        self.assertEqual(entries["share/terminfo/n/ncr260vt300wpp"], b"term")
        self.assertEqual(entries["share/terminfo/N/NCR260VT300WPP"], b"../n/ncr260vt300wpp")

    def test_absolute_busybox_link_stays_inside_the_guest(self) -> None:
        blob = _tar_bytes([("usr/bin/yes", "symlink", b"/bin/busybox")])
        entries = cpio_entries(pack_tar_bytes(blob))
        self.assertEqual(entries["usr/bin/yes"], b"../../bin/busybox")

    def test_escaping_link_and_device_node_are_rejected(self) -> None:
        with self.assertRaises(SsiosError):
            pack_tar_bytes(_tar_bytes([("usr/bin/evil", "symlink", b"/../../etc/passwd")]))
        with self.assertRaises(SsiosError):
            pack_tar_bytes(_tar_bytes([("../etc/passwd", "file", b"no")]))
        entries = cpio_entries(pack_tar_bytes(_tar_bytes([("dev/null", "char", None), ("bin/sh", "file", b"sh")])))
        self.assertNotIn("dev/null", entries)
        self.assertEqual(entries["bin/sh"], b"sh")

    def test_python_share_tree_can_be_omitted(self) -> None:
        blob = _tar_bytes(
            [
                ("python/bin/python3", "symlink", b"python3.12"),
                ("python/share/terminfo/n/ncr260vt300wpp", "file", b"term"),
            ]
        )
        entries = cpio_entries(pack_tar_bytes(blob, dest_prefix="opt/python", strip_prefix="python/", skip=("share",)))
        self.assertEqual(entries["opt/python/bin/python3"], b"python3.12")
        self.assertFalse(any("terminfo" in name for name in entries))
