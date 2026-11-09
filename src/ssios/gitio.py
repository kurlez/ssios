"""Git for SSIOS-owned repositories only.

The child process gets a fresh environment and explicit -c overrides. That
does not change the operator's global Git configuration. Hooks, signing,
identity, attributes, and exclude files from the surrounding environment are
not used.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from ssios.errors import SsiosError

_SECRET = re.compile(
    r"(?i)(bearer\s+\S+|token[=:]\s*\S+|api[_-]?key[=:]\s*\S+"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|ghp_[A-Za-z0-9]+|github_pat_\S+"
    r"|x-access-token:[^@\s]+)"
)
_EMPTY_COMMIT = re.compile(r"nothing to commit|no changes added to commit", re.IGNORECASE)


def isolated_git_env(empty_config: Path, home: Path) -> dict[str, str]:
    path = os.environ.get("PATH", "/usr/bin:/bin")
    return {
        "PATH": path,
        "HOME": str(home),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(empty_config),
        "GIT_CONFIG_COUNT": "0",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "SSIOS",
        "GIT_AUTHOR_EMAIL": "ssios-local@example.invalid",
        "GIT_COMMITTER_NAME": "SSIOS",
        "GIT_COMMITTER_EMAIL": "ssios-local@example.invalid",
    }


def sanitize_git_diagnostic(stdout: bytes, stderr: bytes, *, code: int, subcommand: str) -> str:
    text = "\n".join(
        part.decode("utf-8", errors="replace").strip()
        for part in (stderr, stdout)
        if part and part.strip()
    )
    text = _SECRET.sub("[redacted]", text)
    lines = []
    for line in text.splitlines():
        if len(line) > 180:
            line = line[:180] + "..."
        lines.append(line)
        if len(lines) >= 8:
            break
    body = "\n".join(lines).strip()
    prefix = f"git {subcommand} exited {code}"
    if not body:
        return prefix + " with no output"
    return prefix + ": " + body


def _is_empty_commit(stdout: bytes, stderr: bytes) -> bool:
    text = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
    return _EMPTY_COMMIT.search(text) is not None


class GitRunner:
    def __init__(self, hooks_path: Path, empty_config: Path) -> None:
        self.hooks_path = hooks_path
        self.empty_config = empty_config
        self.home = empty_config.parent / "git-home"
        self.blank = empty_config.parent / "git-blank"

    def prepare(self) -> None:
        self.hooks_path.mkdir(parents=True, exist_ok=True)
        self.home.mkdir(parents=True, exist_ok=True)
        self.empty_config.parent.mkdir(parents=True, exist_ok=True)
        if not self.empty_config.exists():
            self.empty_config.write_bytes(b"")
        if not self.blank.exists():
            self.blank.write_bytes(b"")
        os.chmod(self.hooks_path, 0o700)
        os.chmod(self.home, 0o700)

    def env(self) -> dict[str, str]:
        self.prepare()
        return isolated_git_env(self.empty_config, self.home)

    def _overrides(self) -> list[str]:
        pairs = [
            "commit.gpgsign=false",
            "tag.gpgsign=false",
            "user.name=SSIOS",
            "user.email=ssios-local@example.invalid",
            f"core.hooksPath={self.hooks_path}",
            "core.symlinks=false",
            "core.autocrlf=false",
            "core.filemode=false",
            "core.fsmonitor=",
            f"core.attributesFile={self.blank}",
            f"core.excludesFile={self.blank}",
        ]
        args: list[str] = []
        for item in pairs:
            args.extend(["-c", item])
        return args

    def run(self, repo: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        if any(not isinstance(part, str) or "\x00" in part or part == "--global" for part in args):
            raise SsiosError("git_rejected", "git argument rejected", 500)
        cmd = ["git", "--no-pager", "-C", str(repo), *self._overrides(), *args]
        completed = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            shell=False,
            cwd=str(self.home),
            env=self.env(),
        )
        if check and completed.returncode != 0:
            subcommand = args[0] if args else "git"
            raise SsiosError(
                "git_failed",
                sanitize_git_diagnostic(
                    completed.stdout,
                    completed.stderr,
                    code=completed.returncode,
                    subcommand=subcommand,
                ),
                500,
            )
        return completed

    def init_repo(self, repo: Path) -> None:
        self.prepare()
        repo.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            ["git", "--no-pager", "-C", str(repo), *self._overrides(), "init", "-b", "main"],
            check=False,
            capture_output=True,
            shell=False,
            cwd=str(self.home),
            env=self.env(),
        )
        if completed.returncode != 0:
            raise SsiosError(
                "git_failed",
                sanitize_git_diagnostic(completed.stdout, completed.stderr, code=completed.returncode, subcommand="init"),
                500,
            )
        for key, value in (
            ("user.name", "SSIOS"),
            ("user.email", "ssios-local@example.invalid"),
            ("commit.gpgsign", "false"),
            ("tag.gpgsign", "false"),
            ("core.symlinks", "false"),
            ("core.autocrlf", "false"),
            ("core.filemode", "false"),
            ("core.hooksPath", str(self.hooks_path)),
            ("core.attributesFile", str(self.blank)),
            ("core.excludesFile", str(self.blank)),
        ):
            self.run(repo, ["config", key, value])

    def head(self, repo: Path) -> str:
        completed = self.run(repo, ["rev-parse", "HEAD"])
        return completed.stdout.decode().strip()

    def commit_paths(self, repo: Path, paths: list[str], message: str) -> str:
        if not paths:
            raise SsiosError("git_rejected", "empty commit", 500)
        self.run(repo, ["add", "--", *paths])
        completed = self.run(repo, ["commit", "-m", message, "--", *paths], check=False)
        if completed.returncode == 0:
            return self.head(repo)
        if _is_empty_commit(completed.stdout, completed.stderr) and self._matches_head(repo, paths):
            return self.head(repo)
        raise SsiosError(
            "git_failed",
            sanitize_git_diagnostic(completed.stdout, completed.stderr, code=completed.returncode, subcommand="commit"),
            500,
        )

    def remove_path(self, repo: Path, rel: str, message: str) -> str:
        self.run(repo, ["rm", "-f", "--", rel])
        self.run(repo, ["commit", "-m", message, "--", rel])
        return self.head(repo)

    def _matches_head(self, repo: Path, paths: list[str]) -> bool:
        for rel in paths:
            shown = self.run(repo, ["show", f"HEAD:{rel}"], check=False)
            if shown.returncode != 0:
                return False
            try:
                current = (repo / rel).read_bytes()
            except OSError:
                return False
            if current != shown.stdout:
                return False
        return True
