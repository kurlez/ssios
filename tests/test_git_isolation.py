"""Internal Git repositories stay isolated from the operator's Git configuration."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from support import ROOT

from ssios.gitio import GitRunner, sanitize_git_diagnostic
from ssios.memory_store import MemoryStore


class DiagnosticTest(unittest.TestCase):
    def test_stdout_is_kept_and_secrets_are_removed(self) -> None:
        detail = sanitize_git_diagnostic(
            b"On branch main\nnothing to commit, working tree clean\ntoken=abcd github_pat_secret\n",
            b"",
            code=1,
            subcommand="commit",
        )
        self.assertIn("nothing to commit", detail)
        self.assertIn("git commit exited 1", detail)
        self.assertNotIn("github_pat_secret", detail)
        self.assertNotIn("abcd", detail)
        self.assertNotEqual(detail, "git command failed")

    def test_empty_output_names_the_subcommand(self) -> None:
        detail = sanitize_git_diagnostic(b"", b"   ", code=1, subcommand="commit")
        self.assertIn("no output", detail)
        self.assertIn("commit", detail)


class HostileConfigTest(unittest.TestCase):
    def test_signing_hooks_and_identity_do_not_affect_internal_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "user-home"
            home.mkdir()
            hooks = root / "user-hooks"
            hooks.mkdir()
            hook = hooks / "pre-commit"
            hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            hook.chmod(0o755)
            gitconfig = home / ".gitconfig"
            original = (
                "[commit]\n\tgpgsign = true\n"
                "[gpg]\n\tprogram = /nonexistent/ssios-gpg\n"
                f"[core]\n\thooksPath = {hooks}\n"
                "[user]\n\tname = Personal Name\n\temail = person@example.invalid\n"
            )
            gitconfig.write_text(original, encoding="utf-8")
            (home / ".config/git").mkdir(parents=True)
            (home / ".config/git/ignore").write_text("*.md\n", encoding="utf-8")
            (home / ".config/git/attributes").write_text("* filter=explode\n", encoding="utf-8")
            saved = {key: os.environ.get(key) for key in ("HOME", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1")}
            os.environ["HOME"] = str(home)
            os.environ["GIT_CONFIG_COUNT"] = "2"
            os.environ["GIT_CONFIG_KEY_0"] = "commit.gpgsign"
            os.environ["GIT_CONFIG_VALUE_0"] = "true"
            os.environ["GIT_CONFIG_KEY_1"] = "core.hooksPath"
            os.environ["GIT_CONFIG_VALUE_1"] = str(hooks)
            try:
                state = root / "state"
                runner = GitRunner(state / "git-hooks", state / "empty-gitconfig")
                repo = state / "memory" / "demo-alpha" / "repo"
                store = MemoryStore(state, runner, ROOT / "fixtures")
                first = store.seed_project("demo-alpha")
                self.assertEqual(len(first), 40)
                proposal = store.propose(
                    "demo-alpha",
                    first,
                    [
                        {
                            "path": "topics/fixtures/last-summary.md",
                            "content": "---\nname: last-summary\ndescription: \"synthetic\"\n---\n\nNORTHWIND-FIXTURE-NOTE\n",
                        }
                    ],
                    task_id="human",
                )
                second = store.apply(proposal["id"], first, "appr-test", proposal["payload_sha256"])
                self.assertNotEqual(second, first)
                third = runner.commit_paths(
                    repo,
                    ["topics/fixtures/last-summary.md"],
                    "ssios approved memory repeat",
                )
                self.assertEqual(third, second)
                shown = runner.run(repo, ["show", "HEAD:topics/fixtures/last-summary.md"])
                self.assertIn(b"NORTHWIND-FIXTURE-NOTE", shown.stdout)
                self.assertNotIn(b"Personal Name", runner.run(repo, ["log", "-1", "--format=%an"]).stdout)
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
            self.assertEqual(gitconfig.read_text(encoding="utf-8"), original)


class GlobalConfigUntouchedTest(unittest.TestCase):
    def test_sources_do_not_edit_global_git_config(self) -> None:
        for path in (ROOT / "src").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("git config --global", text)
            self.assertNotIn("--system", text)
