"""Run one script of an installed skill in a sandbox. Only after the owner approved that exact run.

The sandbox has no network, sees the skill's own folder read-only and an empty work folder it may write, and
gets none of the account's environment, keys, or files. Files the owner chose to hand over are copied into the
work folder first. A run has a time limit and an output limit, and its processes are stopped when it ends.

* macOS: ``sandbox-exec`` with a profile that denies everything except reading system libraries and the skill,
  and writing the work folder.
* Linux: ``bwrap`` (bubblewrap) with new namespaces for everything, including the network.

When neither is installed, scripts do not run. There is no fallback to running without a sandbox.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ssios.errors import SsiosError
from ssios.skill_pkg import SCRIPT_EXT

TIMEOUT = 60.0
OUTPUT_LIMIT = 64_000
WORK_LIMIT = 50_000_000
MAX_ARGS = 20


def backend() -> str:
    """Which sandbox this computer has: "sandbox-exec", "bwrap", or ""."""
    if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file():
        return "sandbox-exec"
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return "bwrap"
    return ""


def interpreter(kind: str) -> str:
    if kind == "python":
        return shutil.which("python3") or ""
    if kind == "shell":
        return "/bin/sh" if Path("/bin/sh").exists() else ""
    if kind == "node":
        return shutil.which("node") or ""
    return ""


def _bwrap_program(program: str) -> tuple[list[str], str]:
    """The real interpreter, plus a read-only bind when it lives outside the system folders."""
    real = Path(program).resolve()
    prefix = real.parent.parent if real.parent.name == "bin" else real.parent
    roots = [Path(name) for name in ("/usr", "/bin", "/lib", "/lib64", "/sbin") if Path(name).exists()]
    inside = any(real == root or root in real.parents for root in roots)
    binds: list[str] = []
    if prefix.is_dir() and prefix != Path("/") and not inside:
        binds = ["--ro-bind", str(prefix), str(prefix)]
    return binds, str(real)


def _mac_profile(skill: Path, work: Path, program: Path) -> str:
    def lit(path) -> str:
        return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'

    reads = ["/usr", "/System", "/Library", "/bin", "/sbin", "/private/var/db/dyld", "/opt/homebrew", "/usr/local", "/Applications/Xcode.app", "/private/etc", "/dev"]
    real = program.resolve()
    reads.append(str(real.parent.parent))
    return "\n".join(
        [
            "(version 1)",
            "(deny default)",
            "(allow process-exec process-fork)",
            "(allow signal (target same-sandbox))",
            "(allow sysctl-read)",
            "(allow file-read-metadata)",
            "(allow file-read* " + " ".join(f"(subpath {lit(path)})" for path in reads) + f" (subpath {lit(skill)}) (subpath {lit(work)}))",
            f"(allow file-write* (subpath {lit(work)}) (literal \"/dev/null\"))",
            "(deny network*)",
        ]
    )


def _limits() -> None:
    import resource

    os.setsid()
    for name, value in (("RLIMIT_CPU", int(TIMEOUT) + 5), ("RLIMIT_FSIZE", WORK_LIMIT), ("RLIMIT_NOFILE", 256), ("RLIMIT_CORE", 0)):
        limit = getattr(resource, name, None)
        if limit is not None:
            try:
                resource.setrlimit(limit, (value, value))
            except (ValueError, OSError):
                pass


def run(skill_dir: Path, script: str, args: list[str], stdin: str = "", inputs: dict[str, bytes] | None = None, timeout: float = TIMEOUT) -> dict:
    """Run ``script`` (a path inside the skill) and return its exit code, output, and the files it wrote."""
    where = backend()
    if not where:
        raise SsiosError("skill_sandbox", "This computer has no sandbox for skill scripts (sandbox-exec on a Mac, bubblewrap on Linux). Scripts do not run.", 503)
    skill = Path(skill_dir).resolve()
    target = (skill / script).resolve()
    if skill not in target.parents or not target.is_file() or (skill / script).is_symlink():
        raise SsiosError("skill_rejected", "That script is not part of the skill.", 404)
    kind = SCRIPT_EXT.get(target.suffix.casefold(), "")
    program = interpreter(kind)
    if not program:
        raise SsiosError("skill_sandbox", f"No {kind or 'suitable'} interpreter is installed for this script.", 503)
    values = [str(value)[:2000] for value in (args or [])][:MAX_ARGS]
    with tempfile.TemporaryDirectory(prefix="ssios-skill-") as made:
        work = Path(made).resolve()
        for name, data in (inputs or {}).items():
            safe = Path(str(name)).name.lstrip(".")
            if safe:
                (work / safe).write_bytes(data)
        before = {path.name for path in work.iterdir()}
        relative = target.relative_to(skill).as_posix()
        env = {"PATH": "/usr/bin:/bin", "HOME": "/work" if where == "bwrap" else str(work), "TMPDIR": "/work" if where == "bwrap" else str(work), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
        if where == "bwrap":
            command = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--clearenv"]
            for key, value in env.items():
                command += ["--setenv", key, value]
            for folder in ("/usr", "/etc/alternatives", "/etc/ssl"):
                if Path(folder).exists():
                    command += ["--ro-bind", folder, folder]
            for name in ("lib", "lib64", "bin", "sbin"):
                path = Path("/" + name)
                if path.is_symlink():
                    command += ["--symlink", os.readlink(path), "/" + name]
                elif path.is_dir():
                    command += ["--ro-bind", str(path), "/" + name]
            command += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--ro-bind", str(skill), "/skill", "--bind", str(work), "/work", "--chdir", "/work"]
            extra, program = _bwrap_program(program)
            command += extra
            command += [program, "/skill/" + relative, *values]
        else:
            command = ["/usr/bin/sandbox-exec", "-p", _mac_profile(skill, work, Path(program)), program, str(target), *values]
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            cwd=str(work),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=_limits,
            close_fds=True,
        )
        timed_out = False
        try:
            out, err = process.communicate(stdin.encode("utf-8")[:1_000_000], timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
            out, err = process.communicate()
        seconds = round(time.monotonic() - started, 2)
        produced = []
        for path in sorted(work.rglob("*")):
            if path.is_file() and not path.is_symlink() and path.relative_to(work).as_posix() not in before:
                produced.append({"name": path.relative_to(work).as_posix(), "size": path.stat().st_size, "data": path.read_bytes()[:WORK_LIMIT]})
    return {
        "backend": where,
        "exit": None if timed_out else process.returncode,
        "timed_out": timed_out,
        "stdout": out[:OUTPUT_LIMIT].decode("utf-8", "replace"),
        "stderr": err[:OUTPUT_LIMIT].decode("utf-8", "replace"),
        "seconds": seconds,
        "files": produced[:50],
    }
