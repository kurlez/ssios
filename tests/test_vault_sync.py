"""Two-way sync between Notes and an Obsidian folder: no overwrite without a known change, and no delete."""

from __future__ import annotations

import tempfile
from pathlib import Path

from ssios.errors import SsiosError
from ssios.vault import account_vault, set_vault
from ssios.vault_sync import sync_vault
from support import PlaneTest
from test_library import _png


class VaultSyncTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.account = self.plane.accounts.add_member("ada", "other-horse-stable")["id"]
        self.mine = account_vault(self.plane.settings, self.account)
        made = tempfile.TemporaryDirectory()
        self.addCleanup(made.cleanup)
        self.theirs = Path(made.name).resolve() / "Obsidian"
        (self.mine / "Clippings").mkdir(parents=True)
        (self.theirs / "Daily").mkdir(parents=True)
        (self.theirs / ".obsidian").mkdir()
        (self.mine / "Clippings" / "pier.md").write_text("pier v1\n", encoding="utf-8")
        (self.theirs / "Daily" / "day.md").write_text("day v1\n", encoding="utf-8")
        (self.theirs / "Daily" / "dock.png").write_bytes(_png(3))
        (self.theirs / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
        (self.theirs / "script.sh").write_text("echo no\n", encoding="utf-8")

    def _sync(self, **kwargs) -> dict:
        return sync_vault(self.plane.settings, self.account, str(self.theirs), **kwargs)

    def test_both_directions_then_edits_conflicts_and_deletes(self) -> None:
        plan = self._sync(dry_run=True)
        self.assertEqual(plan["counts"], {"to_obsidian": 1, "to_notes": 2, "conflicts": 0, "trashed_in_notes": 0, "trashed_in_obsidian": 0})
        self.assertFalse((self.theirs / "Clippings").exists())
        first = self._sync()
        self.assertEqual((first["to_obsidian"], sorted(first["to_notes"])), (["Clippings/pier.md"], ["Daily/day.md", "Daily/dock.png"]))
        self.assertEqual((self.theirs / "Clippings" / "pier.md").read_text(encoding="utf-8"), "pier v1\n")
        self.assertEqual((self.mine / "Daily" / "dock.png").read_bytes(), _png(3))
        self.assertFalse((self.mine / ".obsidian").exists())
        self.assertFalse((self.mine / "script.sh").exists())
        self.assertEqual(self._sync()["counts"], {"to_obsidian": 0, "to_notes": 0, "conflicts": 0, "trashed_in_notes": 0, "trashed_in_obsidian": 0})
        # One side edits each file.
        (self.theirs / "Clippings" / "pier.md").write_text("pier edited in Obsidian\n", encoding="utf-8")
        (self.mine / "Daily" / "day.md").write_text("day edited in SSIOS\n", encoding="utf-8")
        second = self._sync()
        self.assertEqual((second["to_notes"], second["to_obsidian"]), (["Clippings/pier.md"], ["Daily/day.md"]))
        self.assertEqual((self.mine / "Clippings" / "pier.md").read_text(encoding="utf-8"), "pier edited in Obsidian\n")
        self.assertEqual((self.theirs / "Daily" / "day.md").read_text(encoding="utf-8"), "day edited in SSIOS\n")
        # Both sides edit the same file. Both versions survive on both sides.
        (self.mine / "Daily" / "day.md").write_text("mine\n", encoding="utf-8")
        (self.theirs / "Daily" / "day.md").write_text("theirs\n", encoding="utf-8")
        third = self._sync(now=0)
        self.assertEqual(third["conflicts"], ["Daily/day.md"])
        self.assertEqual((self.mine / "Daily" / "day.md").read_text(encoding="utf-8"), "mine\n")
        self.assertEqual((self.theirs / "Daily" / "day.md").read_text(encoding="utf-8"), "theirs\n")
        copies = sorted(path.name for path in (self.mine / "Daily").glob("day (conflict*"))
        self.assertEqual(len(copies), 1)
        self.assertIn("conflict from Obsidian", copies[0])
        self.assertEqual((self.mine / "Daily" / copies[0]).read_text(encoding="utf-8"), "theirs\n")
        self.assertEqual(len(list((self.theirs / "Daily").glob("day (conflict from SSIOS*"))), 1)
        # A note deleted in Obsidian goes to the Notes trash. A note deleted there but edited here comes back.
        (self.theirs / "Clippings" / "pier.md").unlink()
        (self.theirs / "Daily" / "dock.png").unlink()
        (self.mine / "Daily" / "dock.png").write_bytes(_png(4))
        fourth = self._sync()
        self.assertEqual(fourth["trashed_in_notes"], ["Clippings/pier.md"])
        self.assertFalse((self.mine / "Clippings" / "pier.md").exists())
        self.assertEqual((self.mine / ".trash" / "Clippings" / "pier.md").read_text(encoding="utf-8"), "pier edited in Obsidian\n")
        self.assertIn("Daily/dock.png", fourth["to_obsidian"])
        self.assertEqual((self.theirs / "Daily" / "dock.png").read_bytes(), _png(4))
        self.assertFalse((self.theirs / "Clippings" / "pier.md").exists())

    def test_folders_that_cannot_be_synced(self) -> None:
        for bad in ("relative", "smb://nas/vault", str(self.state), str(self.theirs / "missing"), str(self.mine), str(self.mine / "Clippings")):
            with self.assertRaises(SsiosError) as caught:
                sync_vault(self.plane.settings, self.account, bad)
            self.assertEqual(caught.exception.code, "sync_folder", bad)
        inside = self.theirs.parent / "into-state"
        inside.symlink_to(self.state / "accounts")
        with self.assertRaises(SsiosError):
            sync_vault(self.plane.settings, self.account, str(inside))
        # ~/Google Drive on a Mac is a link. The sync follows it and names the real folder.
        link = self.theirs.parent / "Google Drive"
        link.symlink_to(self.theirs)
        self.assertEqual(sync_vault(self.plane.settings, self.account, str(link), dry_run=True)["obsidian"], str(self.theirs))
        (self.theirs / "Daily" / "linked.md").symlink_to(self.theirs / "Daily" / "day.md")
        self.assertNotIn("Daily/linked.md", self._sync()["to_notes"])
        set_vault(self.plane.settings, self.account, str(self.theirs))
        with self.assertRaises(SsiosError):
            self._sync()

    def test_the_command_prints_the_plan(self) -> None:
        import contextlib
        import io
        import json

        from ssios.cli import main

        config = str(Path(__file__).resolve().parents[1] / "config" / "ssios.example.toml")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["vault-sync", "--state", str(self.state), "--config", config, "--username", "ada", "--with", str(self.theirs), "--dry-run"])
        self.assertEqual(code, 0, buffer.getvalue())
        self.assertEqual(json.loads(buffer.getvalue())["counts"]["to_notes"], 2)
        self.assertFalse((self.mine / "Daily").exists())
