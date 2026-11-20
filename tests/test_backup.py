"""Backup and restore: checked archive, nothing overwritten, and no path outside the destination."""

from __future__ import annotations

import io
import json
import os
import stat
import tarfile
import tempfile
import unittest
from pathlib import Path

from ssios.backup import create_backup, restore_backup, verify_backup
from ssios.cli import main
from ssios.errors import SsiosError


class BackupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state = root / "state"
        self.out = root / "backups"
        (self.state / "accounts" / "acct-1" / "memory").mkdir(parents=True)
        (self.state / "accounts" / "acct-1" / "empty").mkdir()
        (self.state / "accounts" / "registry.json").write_text('{"users": []}', encoding="utf-8")
        note = self.state / "accounts" / "acct-1" / "memory" / "human.md"
        note.write_text("- name: Harbor\n", encoding="utf-8")
        os.chmod(note, 0o600)
        (self.state / "run").mkdir()
        (self.state / "run" / "ui.log").write_text("log line", encoding="utf-8")
        (self.state / "cache").mkdir()
        (self.state / "cache" / "kernel").write_bytes(b"\x00" * 64)
        (self.state / "listen.url").write_text("http://127.0.0.1:1/", encoding="utf-8")
        os.symlink("/etc/hostname", self.state / "accounts" / "link")

    def _names(self, archive: str) -> list[str]:
        with tarfile.open(archive, "r:gz") as tar:
            return tar.getnames()

    def test_a_backup_round_trips_and_leaves_out_run_cache_and_links(self) -> None:
        made = create_backup(self.state, self.out, now=0)
        archive = Path(made["archive"])
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        self.assertEqual(made["files"], 2)
        self.assertEqual(made["skipped"], ["accounts/link"])
        names = self._names(made["archive"])
        self.assertIn("state/accounts/acct-1/memory/human.md", names)
        self.assertFalse(any("run/" in name or "cache/" in name or "listen.url" in name or "link" in name for name in names))
        self.assertTrue(verify_backup(archive)["ok"])
        dest = Path(self.tmp.name) / "restored"
        report = restore_backup(archive, dest)
        self.assertEqual(report["files"], 2)
        restored = dest / "accounts" / "acct-1" / "memory" / "human.md"
        self.assertEqual(restored.read_text(encoding="utf-8"), "- name: Harbor\n")
        self.assertEqual(stat.S_IMODE(restored.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((dest / "accounts").stat().st_mode), 0o700)
        self.assertTrue((dest / "accounts" / "acct-1" / "empty").is_dir())
        with self.assertRaises(SsiosError) as caught:
            restore_backup(archive, dest)
        self.assertEqual(caught.exception.code, "restore_rejected")
        with self.assertRaises(SsiosError):
            create_backup(self.state, self.state / "inside")

    def test_a_running_ui_blocks_the_backup_unless_allowed(self) -> None:
        (self.state / "run" / "ui.pid").write_text(str(os.getpid()), encoding="utf-8")
        with self.assertRaises(SsiosError) as caught:
            create_backup(self.state, self.out)
        self.assertEqual(caught.exception.code, "backup_running")
        self.assertEqual(list(self.out.glob("*")) if self.out.exists() else [], [])
        self.assertEqual(create_backup(self.state, self.out, allow_running=True)["files"], 2)

    def _forge(self, member: str, body: bytes, manifest_files: list[dict]) -> Path:
        path = Path(self.tmp.name) / "forged.tar.gz"
        with tarfile.open(path, "w:gz") as tar:
            for name, data in ((member, body), ("manifest.json", json.dumps({"format": 1, "files": manifest_files, "dirs": []}).encode())):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return path

    def test_a_changed_or_escaping_archive_is_refused_before_anything_is_written(self) -> None:
        import hashlib

        body = b"x"
        good = hashlib.sha256(body).hexdigest()
        dest = Path(self.tmp.name) / "fresh"
        cases = (
            self._forge("state/../escape.txt", body, [{"path": "../escape.txt", "size": 1, "mode": 0o600, "sha256": good}]),
            self._forge("state/a.txt", body, [{"path": "a.txt", "size": 1, "mode": 0o600, "sha256": "0" * 64}]),
            self._forge("state/a.txt", body, []),
            self._forge("elsewhere/a.txt", body, [{"path": "a.txt", "size": 1, "mode": 0o600, "sha256": good}]),
        )
        for archive in cases:
            with self.assertRaises(SsiosError) as caught:
                restore_backup(archive, dest)
            self.assertEqual(caught.exception.code, "backup_invalid")
            self.assertFalse(dest.exists())
        self.assertFalse((Path(self.tmp.name) / "escape.txt").exists())

    def test_the_commands_print_a_report_and_an_error_code(self) -> None:
        import contextlib

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["backup", "--state", str(self.state), "--out", str(self.out)])
        self.assertEqual(code, 0)
        archive = json.loads(buffer.getvalue())["archive"]
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(main(["backup-verify", archive]), 0)
            self.assertEqual(main(["restore", archive, "--into", str(self.state)]), 1)
        self.assertIn("restore_rejected", buffer.getvalue())


class LaunchdTest(unittest.TestCase):
    def test_the_plist_is_printed_for_the_gui_session_and_does_not_restart_after_stop(self) -> None:
        import plistlib

        from ssios.cli import launchd_plist

        body = plistlib.loads(launchd_plist(Path("/tmp/ssios state"), Path("/tmp/ssios state/ollama.toml")).encode("utf-8"))
        self.assertEqual(body["ProgramArguments"][1], "fg")
        self.assertTrue(body["ProgramArguments"][0].endswith("scripts/ssios"))
        self.assertEqual(body["EnvironmentVariables"]["SSIOS_STATE"], str(Path("/tmp/ssios state").resolve()))
        self.assertEqual(body["LimitLoadToSessionType"], "Aqua")
        self.assertTrue(body["RunAtLoad"])
        self.assertFalse(body["KeepAlive"])


class AtomicWriteTest(unittest.TestCase):
    def test_many_writers_to_one_file_all_succeed_and_leave_whole_json(self) -> None:
        import threading

        from ssios.util import read_json, write_json

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "approvals.json"
            errors: list[BaseException] = []

            def work(index: int) -> None:
                try:
                    for turn in range(40):
                        write_json(path, {"writer": index, "turn": turn, "pad": "x" * 4000})
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=work, args=(index,)) for index in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(read_json(path, None)["turn"], 39)
            self.assertEqual([item.name for item in path.parent.iterdir()], ["approvals.json"])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
