"""vfkit argv stays without a NIC. The guest image packer does not need a Mac."""

from __future__ import annotations

import gzip
import unittest
from pathlib import Path

from ssios.errors import SsiosError
from ssios.macos_vz import assert_no_nic, pack_newc, vfkit_command


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
