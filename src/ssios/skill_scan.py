"""The safety check a skill passes before it can be installed.

It reads every file and reports what it found, line by line, so the owner decides with the evidence in front
of them. Nothing is run. The check looks for three kinds of trouble:

* The package itself: files that are not text, very large files, hidden files, executables.
* The instructions: text that tries to steer the model against the user (ignore earlier instructions, keep
  something secret, send data to an address), hidden characters, and comments a reader would not see.
* The scripts: network access, starting other programs, reading keys and passwords, writing outside the work
  folder, installing packages, staying on the computer after the run, and obfuscated code.

The result is ``low`` (nothing found, or only notes), ``medium`` (warnings the owner must accept), or ``high``
(refused). Scripts never run outside the sandbox, which also has no network, whatever the level.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from ssios.skill_pkg import SCRIPT_EXT, parse_frontmatter

TEXT_EXT = {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".csv", ".tsv", ".xml", ".html", ".css", ".py", ".sh", ".bash", ".js", ".mjs", ".ts", ".ini", ".cfg", ".svg", ".tex", ".rst"}
BINARY_OK = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".ttf", ".otf", ".woff", ".woff2"}

# (severity, code, pattern, what it means). "high" refuses, "medium" warns, "note" informs.
INSTRUCTION_RULES = [
    ("high", "override", r"\b(ignore|disregard|forget)\b[^.\n]{0,40}\b(previous|prior|above|earlier|system|all)\b[^.\n]{0,20}\b(instructions?|rules?|prompts?)\b", "asks the model to ignore its instructions"),
    ("high", "override", r"(忽略|无视|忘记)[^。\n]{0,20}(之前|以上|先前|系统)[^。\n]{0,10}(指令|指示|规则|提示)", "asks the model to ignore its instructions"),
    ("high", "secrecy", r"\b(do not|don't|never)\s+(tell|inform|show|mention|reveal to|let)\s+(the\s+)?(user|owner|human)(\s+know)?\s+(about|that (you|this|it)|what (you|this)|you)\b|\b(keep|hide)\b[^.\n]{0,30}\b(secret|hidden)\s+from\s+(the\s+)?(user|owner)\b|\bwithout\s+(telling|informing|asking)\s+(the\s+)?(user|owner)\b", "asks the model to hide something from the user"),
    ("high", "secrecy", r"(不要|别|切勿)(告诉|透露|让)[^。\n]{0,10}(用户|主人)", "asks the model to hide something from the user"),
    ("high", "exfiltrate", r"\b(send|upload|post|forward|exfiltrate|transmit)\b[^.\n]{0,60}\b(password|token|api[_ -]?key|secret|credential|cookie|private key|ssh key|memory|notes|emails?)\b[^.\n]{0,60}(https?://|webhook|\b[\w.-]+@[\w-]+\.\w+|\bto (this|that|the following|my|our) (url|address|server|endpoint|site|email))", "asks for private data to be sent somewhere"),
    ("high", "exfiltrate", r"(发送|上传|转发)[^。\n]{0,30}(密码|令牌|密钥|凭据|cookie|记忆|邮件)", "asks for private data to be sent somewhere"),
    ("high", "exfiltrate", r"(密码|令牌|密钥|凭据|cookie|记忆|邮件|笔记)[^。\n]{0,20}(发送|上传|转发|发到|传到|提交)", "asks for private data to be sent somewhere"),
    ("medium", "approval", r"\b(without|skip|bypass)\b[^.\n]{0,20}\b(approval|confirmation|asking|permission)\b", "asks to act without the user's approval"),
    ("medium", "role", r"\byou are (now|no longer)\b|\bnew system prompt\b|\bdeveloper mode\b|\bjailbreak\b", "tries to change who the model is"),
    ("medium", "credentials", r"\b(password|api[_ -]?key|access token|secret key)\b[^.\n]{0,30}\b(enter|provide|paste|give)\b", "asks for a password or key"),
]
SCRIPT_RULES = [
    ("medium", "network", r"\b(requests|urllib\.request|urllib3|http\.client|httpx|aiohttp|socket|fetch\(|XMLHttpRequest|curl|wget|nc |netcat|ftplib|smtplib|paramiko)\b", "uses the network (the sandbox has none, so this will fail)"),
    ("medium", "process", r"\b(subprocess|os\.system|os\.popen|os\.exec\w*|pty\.spawn|child_process|execSync|spawnSync|(?<![\w.])eval\(|(?<![\w.])exec\()", "starts other programs or runs code it builds"),
    ("high", "secrets", r"(\.ssh/|id_rsa|id_ed25519|\.aws/|\.gnupg|Keychain|keychain|security find-|login\.keychain|\.netrc|\.git-credentials|/etc/shadow|Cookies\.binarycookies|Login Data)", "reads keys, passwords, or browser data"),
    ("high", "persist", r"\b(crontab|launchctl|LaunchAgents|LaunchDaemons|systemctl\s+(enable|start)|\.bashrc|\.zshrc|\.profile|rc\.local|schtasks)\b", "tries to stay on the computer after the run"),
    ("high", "destroy", r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+(/|~/?|\$HOME/?)(\*|\s|$)|\bmkfs\b|\bdd\s+if=.*of=/dev/|shutil\.rmtree\(\s*['\"](/|~)", "deletes files outside its work folder"),
    ("high", "obfuscate", r"(base64\.b64decode|atob|codecs\.decode|bytes\.fromhex|fromCharCode)[^\n]{0,80}(exec|eval|Function|compile)|(exec|eval)\([^\n]{0,40}(b64decode|decode\(|fromhex)", "runs hidden code"),
    ("medium", "install", r"\b(pip3?\s+install|npm\s+(i|install)|brew\s+install|apt(-get)?\s+install|gem\s+install|curl[^\n|]*\|\s*(sh|bash))\b", "installs software"),
    ("medium", "escape", r"(\.\./\.\.|/Users/|/home/|~/|\$HOME|os\.path\.expanduser|/private/|/etc/)", "touches paths outside its work folder"),
    ("note", "env", r"\b(os\.environ|process\.env|getenv)\b", "reads environment variables (the sandbox passes none of yours)"),
]
COMMENT_MARKS = ("#", "//", "*", "/*", '"' * 3, "'" * 3)
_HIDDEN = re.compile("[​‌‍⁠﻿‪-‮⁦-⁩\U000e0000-\U000e007f]")
_URL = re.compile(r"https?://[^\s<>()\"'`]+")
_COMMENT = re.compile(r"<!--([\s\S]*?)-->")
_LONG_BLOB = re.compile(r"[A-Za-z0-9+/=]{400,}")


def _line(text: str, at: int) -> int:
    return text.count("\n", 0, at) + 1


def _snippet(text: str, at: int) -> str:
    start = text.rfind("\n", 0, at) + 1
    end = text.find("\n", at)
    return " ".join(text[start : end if end >= 0 else len(text)].split())[:160]


def _quoted(text: str, at: int) -> bool:
    start = text.rfind("\n", 0, at) + 1
    before = text[start:at]
    for mark in ('"', "`", "\u201c", "「"):
        close = {"\u201c": "\u201d", "「": "」"}.get(mark, mark)
        if mark == close and before.count(mark) % 2 == 1:
            return True
        if mark != close and before.count(mark) > before.count(close):
            return True
    return False


def scan(files: dict[str, bytes]) -> dict:
    findings: list[dict] = []
    urls: set[str] = set()

    def add(severity: str, code: str, path: str, line: int, why: str, text: str = "") -> None:
        findings.append({"severity": severity, "code": code, "path": path, "line": line, "why": why, "text": text})

    for path, data in sorted(files.items()):
        suffix = PurePosixPath(path).suffix.casefold()
        parts = PurePosixPath(path).parts
        if any(part.startswith(".") for part in parts):
            add("medium", "hidden", path, 0, "a hidden file")
        if suffix in BINARY_OK:
            continue
        if suffix in {".exe", ".dll", ".so", ".dylib", ".bin", ".app", ".pkg", ".dmg", ".jar", ".pyc", ".class", ".wasm"} or data[:4] in {b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"MZ\x90\x00"}:
            add("high", "binary", path, 0, "a compiled program, which cannot be reviewed")
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            add("medium", "unreadable", path, 0, "not text, and not a known image or document")
            continue
        if suffix and suffix not in TEXT_EXT:
            add("note", "other", path, 0, "a file type the check does not know")
        for match in _HIDDEN.finditer(text):
            if match.start() == 0 and match.group() == "\ufeff":
                continue  # a byte-order mark at the start of a file is ordinary
            add("high", "hidden-text", path, _line(text, match.start()), "contains invisible characters that can hide instructions", f"U+{ord(match.group()):04X}")
            break
        for match in _URL.finditer(text):
            urls.add(match.group().rstrip(".,;:"))
        is_script = suffix in SCRIPT_EXT
        if suffix in {".md", ".txt", ""} or path == "SKILL.md":
            for severity, code, pattern, why in INSTRUCTION_RULES:
                for match in re.finditer(pattern, text, re.IGNORECASE):
                    # Quoted words are usually an example of what not to write. The owner still sees them.
                    level = "medium" if severity == "high" and _quoted(text, match.start()) else severity
                    add(level, code, path, _line(text, match.start()), why + (" (quoted)" if level != severity else ""), _snippet(text, match.start()))
            for match in _COMMENT.finditer(text):
                body = " ".join(match.group(1).split())
                # A comment inside a code example is part of the example, not something hidden from the reader.
                if text.count("```", 0, match.start()) % 2 == 1:
                    continue
                if len(body) > 20:
                    add("medium", "comment", path, _line(text, match.start()), "a comment the page does not show", body[:160])
        if is_script or "```" in text:
            for severity, code, pattern, why in SCRIPT_RULES:
                for match in re.finditer(pattern, text):
                    # A pattern inside a Markdown file is an example; only a script is something that runs.
                    level = severity if is_script else ("note" if severity != "high" else "medium")
                    line_start = text.rfind("\n", 0, match.start()) + 1
                    if is_script and text[line_start : match.start()].lstrip().startswith(COMMENT_MARKS):
                        level = "note"  # a comment explains; it does not run
                    add(level, code, path, _line(text, match.start()), why, _snippet(text, match.start()))
        if is_script and _LONG_BLOB.search(text):
            match = _LONG_BLOB.search(text)
            add("medium", "blob", path, _line(text, match.start()), "a long encoded block that cannot be read")
        if is_script and data.startswith(b"#!") and b"\n" in data:
            first = data.split(b"\n", 1)[0].decode("utf-8", "replace")
            if not re.search(r"(python3?|bash|sh|node)\b", first):
                add("medium", "interpreter", path, 1, "asks for an interpreter the sandbox does not offer", first[:80])
    if "SKILL.md" in files:
        head, _body = parse_frontmatter(files["SKILL.md"].decode("utf-8", "replace"))
        tools = str(head.get("allowed-tools") or "")
        if re.search(r"\b(Bash|Write|Edit|WebFetch|Computer)\b", tools):
            add("note", "tools", "SKILL.md", 1, "names tools of another program; SSIOS offers only its own tools", tools[:160])
    order = {"high": 0, "medium": 1, "note": 2}
    findings.sort(key=lambda row: (order[row["severity"]], row["path"], row["line"]))
    # One line per rule and file is enough to decide; the count says how many more.
    seen: dict[tuple, dict] = {}
    for row in findings:
        key = (row["severity"], row["code"], row["path"])
        if key in seen:
            seen[key]["more"] = seen[key].get("more", 0) + 1
        else:
            seen[key] = row
    kept = list(seen.values())
    level = "high" if any(row["severity"] == "high" for row in kept) else "medium" if any(row["severity"] == "medium" for row in kept) else "low"
    return {"level": level, "findings": kept[:200], "urls": sorted(urls)[:50], "files": len(files), "bytes": sum(len(data) for data in files.values())}
