"""Install a skill from a link: fetch it through the host broker, unpack, scan, and stage it for the owner.

Links that work:

* a GitHub folder, ``https://github.com/owner/repo/tree/main/skills/pdf``, or a whole repository;
* a GitHub SKILL.md page, ``…/blob/main/skills/pdf/SKILL.md`` (its folder is installed);
* a ``.zip`` anywhere public;
* a plain SKILL.md (a skill with no other files).

The fetch uses the same public-only rules as other web reads and is off while an admin has web reads off.
"""

from __future__ import annotations

import re
from urllib.parse import quote, urlsplit

from ssios.errors import SsiosError
from ssios.skill_pkg import MAX_DOWNLOAD, from_zip, stage, validate
from ssios.skill_scan import scan

_GITHUB = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:/(tree|blob)/([^/]+)(?:/(.*))?)?/?$")


def resolve(url: str) -> tuple[str, str, str]:
    """(address to fetch, folder inside the archive, kind) for a link the owner pasted."""
    text = str(url or "").strip()
    if not re.match(r"^https://", text, re.I) or len(text) > 1000:
        raise SsiosError("skill_rejected", "Paste an https:// link to a skill.", 400)
    found = _GITHUB.match(text.split("#", 1)[0].split("?", 1)[0])
    if found:
        owner, repo, mode, ref, path = found.groups()
        path = (path or "").strip("/")
        if mode == "blob":
            if not path.endswith("SKILL.md"):
                raise SsiosError("skill_rejected", "Link to a skill's folder or to its SKILL.md.", 400)
            path = path[: -len("SKILL.md")].strip("/")
        ref = ref or "HEAD"
        return f"https://codeload.github.com/{owner}/{repo}/zip/{quote(ref, safe='')}", path, "github"
    path = urlsplit(text).path.casefold()
    if path.endswith(".zip"):
        return text, "", "zip"
    return text, "", "file"


def fetch_skill(plane, account_id: str, url: str, peer_ip: str) -> dict:
    """Fetch, check, and stage a skill. Returns the staged record with its safety report."""
    broker = plane.skills
    policy = broker.policy()
    if not policy.get("enabled"):
        raise SsiosError("skill_rejected", "Web reads are off on this computer, so a skill cannot be fetched. An admin can turn them on.", 403)
    address, subpath, kind = resolve(url)
    fetched = broker.fetch(address, dict(policy, max_bytes=MAX_DOWNLOAD, timeout=40.0), account_id, "skill_install", peer_ip)
    body = fetched["body"]
    if len(body) > MAX_DOWNLOAD:
        raise SsiosError("skill_rejected", "The skill is larger than SSIOS accepts.", 400)
    if body[:4] == b"PK\x03\x04":
        files = from_zip(body, subpath)
    elif kind == "file":
        files = {"SKILL.md": body}
    else:
        raise SsiosError("skill_rejected", "That link did not return a zip file.", 400)
    validate(files)
    report = scan(files)
    record = stage(plane, account_id, files, str(url).strip(), report)
    plane.security_audit.write(actor=account_id, ip=peer_ip, action="skill.fetch", target=record["name"], result=report["level"], extra={"item_count": str(report["files"])})
    return record
