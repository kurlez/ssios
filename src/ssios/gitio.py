"""Git invocations for the memory repository. Argv only, hooks disabled."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ssios.errors import SsiosError


class GitRunner:
    def __init__(self, hooks_path: Path, empty_config: Path) -> None:
        self.hooks_path = hooks_path
        self.empty_config = empty_config

    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = str(self.empty_config)
        env["GIT_PAGER"] = "cat"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_AUTHOR_NAME"] = "SSIOS"
        env["GIT_AUTHOR_EMAIL"] = "ssios-local@example.invalid"
        env["GIT_COMMITTER_NAME"] = "SSIOS"
        env["GIT_COMMITTER_EMAIL"] = "ssios-local@example.invalid"
        env.pop("GIT_DIR", None)
        env.pop("GIT_WORK_TREE", None)
        return env

    def run(self, repo: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        cmd = [
            "git",
            "--no-pager",
            "-C",
            str(repo),
            "-c",
            "commit.gpgsign=false",
            "-c",
            f"core.hooksPath={self.hooks_path}",
            "-c",
            "core.symlinks=false",
            *args,
        ]
        if any(not isinstance(part, str) or "\x00" in part for part in args):
            raise SsiosError("git_rejected", "git argument rejected", 500)
        completed = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            shell=False,
            env=self.env(),
        )
        if check and completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()[:300]
            raise SsiosError("git_failed", detail or "git command failed", 500)
        return completed

    def init_repo(self, repo: Path) -> None:
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "-b", "main", str(repo)],
            check=True,
            capture_output=True,
            shell=False,
            env=self.env(),
        )
        for key, value in (
            ("user.name", "SSIOS"),
            ("user.email", "ssios-local@example.invalid"),
            ("commit.gpgsign", "false"),
            ("core.symlinks", "false"),
            ("core.hooksPath", str(self.hooks_path)),
        ):
            self.run(repo, ["config", key, value])

    def head(self, repo: Path) -> str:
        completed = self.run(repo, ["rev-parse", "HEAD"])
        return completed.stdout.decode().strip()

    def commit_paths(self, repo: Path, paths: list[str], message: str) -> str:
        if not paths:
            raise SsiosError("git_rejected", "empty commit", 500)
        self.run(repo, ["add", "--", *paths])
        self.run(repo, ["commit", "-m", message, "--", *paths])
        return self.head(repo)

    def remove_path(self, repo: Path, rel: str, message: str) -> str:
        self.run(repo, ["rm", "-f", "--", rel])
        self.run(repo, ["commit", "-m", message, "--", rel])
        return self.head(repo)
