"""The Skills page: install a skill from a link, read its safety report, and manage what is installed."""

from __future__ import annotations

import html

from ssios.errors import SsiosError
from ssios.ui import Page


def _zh(plane, account_id: str) -> bool:
    try:
        return str(plane.skills.account_prefs(account_id).get("language") or "") == "zh"
    except Exception:
        return False


def _t(zh: bool, en: str, cn: str) -> str:
    return html.escape(cn if zh else en)


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


LEVEL = {"low": ("Passed", "通过"), "medium": ("Warnings", "有警告"), "high": ("Refused", "拒绝安装")}
SEVERITY = {"high": ("Blocks install", "阻止安装"), "medium": ("Warning", "警告"), "note": ("Note", "提示")}


def _size(count: int) -> str:
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{round(count / 1024)} KB"
    return f"{count / 1024 / 1024:.1f} MB"


def _report(report: dict, zh: bool) -> str:
    level = str(report.get("level") or "high")
    rows = []
    for row in report.get("findings") or []:
        where = html.escape(str(row.get("path") or "")) + (f":{int(row['line'])}" if row.get("line") else "")
        more = f' <span class="muted">(+{int(row["more"])})</span>' if row.get("more") else ""
        rows.append(
            f'<tr class="sev-{_esc(row.get("severity"))}"><td>{_t(zh, *SEVERITY.get(str(row.get("severity")), ("", "")))}</td>'
            f"<td><code>{where}</code></td><td>{html.escape(str(row.get('why') or ''))}{more}"
            + (f'<br><code class="snippet">{html.escape(str(row.get("text") or ""))}</code>' if row.get("text") else "")
            + "</td></tr>"
        )
    table = (
        '<div class="skill-scroll"><table class="skill-findings"><thead><tr>'
        + f"<th>{_t(zh, 'Level', '级别')}</th><th>{_t(zh, 'Where', '位置')}</th><th>{_t(zh, 'What', '内容')}</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
        if rows
        else f"<p>{_t(zh, 'Nothing was found.', '没有发现问题。')}</p>"
    )
    urls = report.get("urls") or []
    links = (
        f"<details><summary>{_t(zh, 'Web addresses in the skill', 'skill 里出现的网址')} ({len(urls)})</summary><ul>"
        + "".join(f"<li><code>{html.escape(url)}</code></li>" for url in urls)
        + "</ul></details>"
        if urls
        else ""
    )
    return (
        f'<p><span class="skill-level level-{_esc(level)}">{_t(zh, *LEVEL.get(level, ("?", "?")))}</span> '
        f'{int(report.get("files") or 0)} {_t(zh, "files", "个文件")}, {_size(int(report.get("bytes") or 0))}</p>'
        + table
        + links
    )


def skills_page(plane, session: dict, query: dict, notice: str = "") -> str:
    from ssios.skill_pkg import installed, read_file, staged
    from ssios.skill_sandbox import backend
    from ssios.ui import _page

    account = session["account"]
    account_id = account["id"]
    zh = _zh(plane, account_id)
    admin = str(account.get("role") or "") == "admin"
    csrf = _esc(session.get("csrf") or "")
    hidden = f'<input type="hidden" name="csrf" value="{csrf}">'
    box = backend()
    web_on = bool(plane.skills.policy().get("enabled"))
    intro = (
        f"<p>{_t(zh, 'A skill teaches the local model how to do one kind of task. Its instructions are read only when a request needs them. Scripts in a skill run only in a sandbox with no network, only if you allowed them, and each run waits for your approval.', 'skill 教本地模型完成某一类任务。只有请求用得上时才会读取它的说明。skill 里的脚本只会在没有网络的沙箱里运行，且需要你允许，每次运行前还要你批准。')}</p>"
        + f'<p class="muted small">{_t(zh, "Sandbox on this computer: ", "本机沙箱：")}{html.escape(box) if box else _t(zh, "none, so scripts do not run", "没有，脚本不会运行")}</p>'
    )
    install = (
        f'<form method="post" action="/ui/skills" class="skill-install">{hidden}<input type="hidden" name="op" value="fetch">'
        f'<label>{_t(zh, "Link to a skill", "skill 链接")} <input type="url" name="url" required maxlength="1000" placeholder="https://github.com/owner/repo/tree/main/skills/name"></label>'
        f'<button type="submit">{_t(zh, "Check skill", "检查 skill")}</button></form>'
        f'<p class="muted small">{_t(zh, "A GitHub folder or SKILL.md page, a .zip, or a plain SKILL.md. Nothing is installed until you read the safety report and confirm.", "可以是 GitHub 文件夹或 SKILL.md 页面、.zip 文件，或单独的 SKILL.md。阅读安全报告并确认之前，不会安装任何东西。")}</p>'
    )
    if not web_on:
        install += f'<p class="notice">{_t(zh, "Web reads are off on this computer, so a skill cannot be fetched. An admin can turn them on.", "本机的网页读取已关闭，无法下载 skill。管理员可以打开。")}</p>'
    review = ""
    stage_id = str(query.get("stage") or "")
    if stage_id:
        try:
            record, _files = staged(plane, account_id, stage_id)
        except SsiosError as exc:
            review = f'<p class="notice">{html.escape(exc.detail)}</p>'
        else:
            report = record.get("report") or {}
            level = str(report.get("level") or "high")
            scripts = record.get("scripts") or []
            choices = ""
            if level == "medium":
                choices += f'<label><input type="checkbox" name="accept" value="1" required> {_t(zh, "I read the warnings above and want to install it.", "我已阅读上面的警告，仍要安装。")}</label>'
            if scripts:
                if admin and box:
                    choices += f'<label><input type="checkbox" name="scripts" value="1"> {_t(zh, "Allow its scripts to run in the sandbox. Each run still asks me.", "允许它的脚本在沙箱里运行，每次运行仍要我批准。")}</label>'
                else:
                    choices += f'<p class="muted small">{_t(zh, "Its scripts stay off: only an admin can allow scripts, and only on a computer with a sandbox.", "它的脚本保持关闭：只有管理员能允许脚本，并且本机要有沙箱。")}</p>'
            action = (
                f'<form method="post" action="/ui/skills" class="skill-confirm">{hidden}<input type="hidden" name="op" value="install"><input type="hidden" name="stage" value="{_esc(stage_id)}">'
                f"{choices}<button type=\"submit\" class=\"primary\">{_t(zh, 'Install', '安装')}</button></form>"
                if level != "high"
                else f'<p class="notice">{_t(zh, "This skill cannot be installed: it failed the safety check.", "这个 skill 没有通过安全检查，不能安装。")}</p>'
            )
            review = (
                f'<section class="skill-review"><h2>{html.escape(record["name"])}</h2><p>{html.escape(record["description"])}</p>'
                f'<p class="muted small">{_t(zh, "From", "来源")} <code>{html.escape(record["source"])}</code></p>'
                + (f"<p>{_t(zh, 'Scripts', '脚本')}: " + ", ".join(f"<code>{html.escape(one)}</code>" for one in scripts) + "</p>" if scripts else "")
                + _report(report, zh)
                + action
                + f'<form method="post" action="/ui/skills">{hidden}<input type="hidden" name="op" value="cancel"><input type="hidden" name="stage" value="{_esc(stage_id)}"><button type="submit">{_t(zh, "Cancel", "取消")}</button></form>'
                + "</section>"
            )
    rows = []
    for meta in installed(plane, account_id):
        name = meta["name"]
        state = _t(zh, "On", "已启用") if meta.get("enabled") else _t(zh, "Off", "已停用")
        scripts = meta.get("scripts") or []
        toggles = (
            f'<form method="post" action="/ui/skills">{hidden}<input type="hidden" name="op" value="toggle"><input type="hidden" name="name" value="{_esc(name)}">'
            f'<input type="hidden" name="enabled" value="{"0" if meta.get("enabled") else "1"}"><button type="submit">{_t(zh, "Turn off", "停用") if meta.get("enabled") else _t(zh, "Turn on", "启用")}</button></form>'
        )
        if scripts and admin:
            toggles += (
                f'<form method="post" action="/ui/skills">{hidden}<input type="hidden" name="op" value="scripts"><input type="hidden" name="name" value="{_esc(name)}">'
                f'<input type="hidden" name="allowed" value="{"0" if meta.get("scripts_allowed") else "1"}"><button type="submit">'
                + (_t(zh, "Stop its scripts", "禁止脚本") if meta.get("scripts_allowed") else _t(zh, "Allow its scripts", "允许脚本"))
                + "</button></form>"
            )
        toggles += (
            f'<details class="skill-remove"><summary>{_t(zh, "Remove…", "移除…")}</summary><form method="post" action="/ui/skills">{hidden}<input type="hidden" name="op" value="remove"><input type="hidden" name="name" value="{_esc(name)}">'
            f'<button type="submit" class="lib-danger">{_t(zh, "Remove this skill", "移除这个 skill")}</button></form></details>'
        )
        try:
            body = read_file(plane, account_id, name) if meta.get("enabled") else ""
        except SsiosError:
            body = ""
        script_note = ""
        if scripts:
            script_note = f"<p class=\"muted small\">{_t(zh, 'Scripts', '脚本')}: {', '.join(html.escape(one) for one in scripts)} · " + (_t(zh, "allowed, each run asks you", "已允许，每次运行需批准") if meta.get("scripts_allowed") else _t(zh, "not allowed", "未允许")) + "</p>"
        rows.append(
            f'<article class="skill-card"><h3>{html.escape(name)} <span class="muted small">{state}</span> '
            f'<span class="skill-level level-{_esc((meta.get("report") or {}).get("level") or "low")}">{_t(zh, *LEVEL.get(str((meta.get("report") or {}).get("level") or "low"), ("", "")))}</span></h3>'
            f'<p>{html.escape(str(meta.get("description") or ""))}</p>'
            f'<p class="muted small"><code>{html.escape(str(meta.get("source") or ""))}</code></p>'
            + script_note
            + f'<div class="skill-actions">{toggles}</div>'
            + (f"<details><summary>{_t(zh, 'SKILL.md and safety report', 'SKILL.md 与安全报告')}</summary><pre class=\"skill-md\">{html.escape(body[:20000])}</pre>{_report(meta.get('report') or {}, zh)}</details>" if body or meta.get("report") else "")
            + "</article>"
        )
    listing = f"<h2>{_t(zh, 'Installed', '已安装')}</h2>" + ("".join(rows) or f"<p>{_t(zh, 'No skills yet.', '还没有安装 skill。')}</p>")
    return _page(session, "Skills", notice + intro + install + review + listing)


def skills_post(plane, session: dict, body: dict, peer_ip: str) -> Page:
    from ssios.skill_install import fetch_skill
    from ssios.skill_pkg import drop_stage, install, remove, update
    from ssios.skill_sandbox import backend

    account = session["account"]
    account_id = account["id"]
    zh = _zh(plane, account_id)
    admin = str(account.get("role") or "") == "admin"
    op = str(body.get("op") or "")
    try:
        if op == "fetch":
            record = fetch_skill(plane, account_id, str(body.get("url") or ""), peer_ip)
            return Page(200, skills_page(plane, session, {"stage": record["id"]}))
        if op == "install":
            meta = install(plane, account_id, str(body.get("stage") or ""), allow_scripts=admin and bool(body.get("scripts")) and bool(backend()), accept_warnings=bool(body.get("accept")))
            plane.security_audit.write(actor=account_id, ip=peer_ip, action="skill.install", target=meta["name"], result=str((meta.get("report") or {}).get("level") or ""), extra={"scripts": "on" if meta.get("scripts_allowed") else "off"})
            note = f"已安装 {meta['name']}。" if zh else f"Installed {meta['name']}."
        elif op == "cancel":
            drop_stage(plane, account_id, str(body.get("stage") or ""))
            note = "已取消。" if zh else "Cancelled. Nothing was installed."
        elif op == "toggle":
            meta = update(plane, account_id, str(body.get("name") or ""), enabled=str(body.get("enabled") or "") == "1")
            note = ("已启用。" if zh else "Turned on.") if meta["enabled"] else ("已停用。" if zh else "Turned off.")
        elif op == "scripts":
            if not admin:
                raise SsiosError("skill_rejected", "Only an admin can allow scripts.", 403)
            meta = update(plane, account_id, str(body.get("name") or ""), scripts_allowed=str(body.get("allowed") or "") == "1" and bool(backend()))
            plane.security_audit.write(actor=account_id, ip=peer_ip, action="skill.scripts", target=meta["name"], result="on" if meta["scripts_allowed"] else "off")
            note = ("已允许脚本。" if zh else "Its scripts may run, each after your approval.") if meta["scripts_allowed"] else ("已禁止脚本。" if zh else "Its scripts are off.")
        elif op == "remove":
            name = str(body.get("name") or "")
            remove(plane, account_id, name)
            plane.security_audit.write(actor=account_id, ip=peer_ip, action="skill.remove", target=name, result="ok")
            note = "已移除。" if zh else "Removed."
        else:
            raise SsiosError("skill_rejected", "Unknown action.", 400)
    except SsiosError as exc:
        if not str(exc.code).startswith(("skill_", "mcp_")):
            raise
        stage_id = str(body.get("stage") or "") if op == "install" else ""
        return Page(200, skills_page(plane, session, {"stage": stage_id} if stage_id else {}, f'<p class="notice">{html.escape(exc.detail)}</p>'))
    return Page(200, skills_page(plane, session, {}, f'<p class="notice">{html.escape(note)}</p>'))
