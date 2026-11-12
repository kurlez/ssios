"""Three-pane personal library. Sharing and smart folders are later milestones."""

from __future__ import annotations

import html

from ssios.errors import SsiosError
from ssios.library import (
    add_folder,
    add_tag,
    create_library,
    get_item,
    import_bytes,
    item_bytes,
    library_or_default,
    list_items,
    list_libraries,
    metadata,
    purge_item,
    solid_png,
    update_item,
)
from ssios.locale_messages import say
from ssios.ui import Page, icon

LIBRARY_JS = r"""
(function () {
  var form = document.getElementById("library-import");
  var zone = document.getElementById("library-grid");
  if (!form || !zone || form.getAttribute("data-bound") === "1") return;
  form.setAttribute("data-bound", "1");
  function send(file, source) {
    if (!file) return;
    var data = new FormData(form);
    data.set("file", file);
    data.set("op", "import");
    data.set("source", source);
    fetch(form.action, {method: "POST", body: data, credentials: "same-origin", headers: {Accept: "text/html"}}).then(function () {
      location.reload();
    });
  }
  zone.addEventListener("dragover", function (event) { event.preventDefault(); });
  zone.addEventListener("drop", function (event) {
    event.preventDefault();
    var files = event.dataTransfer && event.dataTransfer.files;
    if (files && files[0]) send(files[0], "drag");
  });
  document.addEventListener("paste", function (event) {
    var files = event.clipboardData && event.clipboardData.files;
    if (files && files[0]) send(files[0], "paste");
  });
})();
"""


def library_script_path() -> str:
    from ssios.ui import _asset_hash

    return "/ui/assets/library-" + _asset_hash(LIBRARY_JS) + ".js"


def library_page(plane, session: dict, query: dict, notice: str = "") -> str:
    from ssios.ui import _page

    account_id = session["account"]["id"]
    lang = _language(plane, account_id)
    chosen = library_or_default(plane, account_id, str(query.get("library") or ""))
    library_id = chosen["id"]
    meta = metadata(plane, account_id, library_id)
    folder = str(query.get("folder") or "")
    tag = str(query.get("tag") or "")
    trash = str(query.get("trash") or "") == "1"
    uncategorized = str(query.get("uncategorized") or "") == "1"
    untagged = str(query.get("untagged") or "") == "1"
    items = list_items(
        plane,
        account_id,
        library_id,
        folder=folder,
        tag=tag,
        trash=trash,
        uncategorized=uncategorized,
        untagged=untagged,
    )
    selected = None
    item_id = str(query.get("item") or "")
    if item_id:
        try:
            selected = get_item(plane, account_id, library_id, item_id)
        except SsiosError:
            selected = None
    csrf = html.escape(str(session.get("csrf") or ""), quote=True)
    base = f"/ui/library?library={html.escape(library_id, quote=True)}"
    switcher = []
    for row in list_libraries(plane, account_id):
        mark = " active" if row["id"] == library_id else ""
        switcher.append(
            f'<a class="lib-switch{mark}" href="/ui/library?library={html.escape(row["id"], quote=True)}">'
            f'{icon("person")}<span>{html.escape(row["name"])}</span>'
            f'<span class="lib-badge">{html.escape(say(lang, "library_personal"))}</span></a>'
        )
    folders = []
    for row in meta.get("folders") or []:
        if not isinstance(row, dict):
            continue
        folders.append(
            f'<a href="{base}&amp;folder={html.escape(str(row.get("id") or ""), quote=True)}">{html.escape(str(row.get("name") or ""))}</a>'
        )
    tags = []
    for name in meta.get("tags") or []:
        tags.append(f'<a href="{base}&amp;tag={html.escape(str(name), quote=True)}">{html.escape(str(name))}</a>')
    cards = []
    for item in items:
        ident = html.escape(str(item.get("id") or ""), quote=True)
        label = html.escape(str(item.get("name") or ""))
        ext = html.escape(str(item.get("ext") or "").upper())
        if item.get("group") == "image" and item.get("ext") in {"png", "jpg", "gif", "webp", "bmp"}:
            thumb = f'<img src="/ui/library/raw?library={html.escape(library_id, quote=True)}&amp;item={ident}" alt="">'
        else:
            thumb = f'<span class="type-tile">{ext}</span>'
        cards.append(
            f'<a class="lib-card" href="{base}&amp;item={ident}">{thumb}<span>{label}</span><span class="type-badge">{ext}</span></a>'
        )
    inspector = "<p>Select an item.</p>"
    if selected:
        ident = html.escape(str(selected.get("id") or ""), quote=True)
        preview = ""
        if selected.get("group") == "image":
            preview = f'<img class="preview" src="/ui/library/raw?library={html.escape(library_id, quote=True)}&amp;item={ident}" alt="">'
        elif selected.get("ext") == "pdf":
            preview = f'<iframe class="preview" sandbox src="/ui/library/raw?library={html.escape(library_id, quote=True)}&amp;item={ident}" title="PDF"></iframe>'
        else:
            preview = f'<p class="type-tile">{html.escape(str(selected.get("ext") or "").upper())}</p>'
        versions = len(selected.get("versions") or [])
        star = int(selected.get("star") or 0)
        inspector = (
            preview
            + f"<h2>{html.escape(str(selected.get('name') or ''))}</h2>"
            + f"<p>{html.escape(str(selected.get('mime') or ''))} · {int(selected.get('size') or 0)} bytes · {versions} version{'s' if versions != 1 else ''}</p>"
            + (f"<p>Captured {html.escape(str(selected.get('captured_at') or ''))}</p>" if selected.get("captured_at") else "")
            + f"<form method=\"post\" action=\"/ui/library\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
            + f"<input type=\"hidden\" name=\"library\" value=\"{html.escape(library_id, quote=True)}\">"
            + f"<input type=\"hidden\" name=\"item\" value=\"{ident}\">"
            + f"<label>Star <input name=\"star\" type=\"number\" min=\"0\" max=\"5\" value=\"{star}\"></label>"
            + "<button name=\"op\" value=\"star\" type=\"submit\">Save rating</button></form>"
            + f"<form method=\"post\" action=\"/ui/library\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
            + f"<input type=\"hidden\" name=\"library\" value=\"{html.escape(library_id, quote=True)}\">"
            + f"<input type=\"hidden\" name=\"item\" value=\"{ident}\">"
            + "<label>Tag <input name=\"tag\" maxlength=\"40\"></label>"
            + "<button name=\"op\" value=\"tag-item\" type=\"submit\">Add tag</button></form>"
        )
        if selected.get("trashed"):
            inspector += (
                f"<form method=\"post\" action=\"/ui/library\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
                f"<input type=\"hidden\" name=\"library\" value=\"{html.escape(library_id, quote=True)}\">"
                f"<input type=\"hidden\" name=\"item\" value=\"{ident}\">"
                "<button name=\"op\" value=\"restore\" type=\"submit\">Restore</button></form>"
                f"<form method=\"post\" action=\"/ui/library\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
                f"<input type=\"hidden\" name=\"library\" value=\"{html.escape(library_id, quote=True)}\">"
                f"<input type=\"hidden\" name=\"item\" value=\"{ident}\">"
                "<label>Type delete to remove the copy <input name=\"confirm\"></label>"
                "<button name=\"op\" value=\"purge\" type=\"submit\">Delete permanently</button></form>"
            )
        else:
            inspector += (
                f"<form method=\"post\" action=\"/ui/library\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
                f"<input type=\"hidden\" name=\"library\" value=\"{html.escape(library_id, quote=True)}\">"
                f"<input type=\"hidden\" name=\"item\" value=\"{ident}\">"
                "<label>Folder <select name=\"folder\">"
                + "".join(
                    f'<option value="{html.escape(str(row.get("id") or ""), quote=True)}">{html.escape(str(row.get("name") or ""))}</option>'
                    for row in meta.get("folders") or []
                    if isinstance(row, dict)
                )
                + "</select></label><button name=\"op\" value=\"assign\" type=\"submit\">Add to folder</button></form>"
                f"<form method=\"post\" action=\"/ui/library\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
                f"<input type=\"hidden\" name=\"library\" value=\"{html.escape(library_id, quote=True)}\">"
                f"<input type=\"hidden\" name=\"item\" value=\"{ident}\">"
                "<button name=\"op\" value=\"trash\" type=\"submit\">Move to trash</button></form>"
            )
    left = (
        '<div class="lib-side"><h2>Libraries</h2>'
        + "".join(switcher)
        + f'<form method="post" action="/ui/library"><input type="hidden" name="csrf" value="{csrf}">'
        + '<label>New library <input name="name" maxlength="40"></label>'
        + '<button name="op" value="create-library" type="submit">Create</button></form>'
        + f'<p><a href="{base}">All</a> <a href="{base}&amp;uncategorized=1">Uncategorized</a> '
        + f'<a href="{base}&amp;untagged=1">Untagged</a> <a href="{base}&amp;trash=1">Trash</a></p>'
        + "<h2>Folders</h2>" + ("".join(f"<p>{row}</p>" for row in folders) or "<p>No folders.</p>")
        + f'<form method="post" action="/ui/library"><input type="hidden" name="csrf" value="{csrf}">'
        + f'<input type="hidden" name="library" value="{html.escape(library_id, quote=True)}">'
        + '<label>Folder <input name="name" maxlength="40"></label>'
        + '<button name="op" value="folder" type="submit">Add folder</button></form>'
        + "<h2>Tags</h2>" + ("".join(f"<p>{row}</p>" for row in tags) or "<p>No tags.</p>")
        + f'<form method="post" action="/ui/library"><input type="hidden" name="csrf" value="{csrf}">'
        + f'<input type="hidden" name="library" value="{html.escape(library_id, quote=True)}">'
        + '<label>Tag <input name="name" maxlength="40"></label>'
        + '<button name="op" value="tag" type="submit">Add tag</button></form></div>'
    )
    grid = (
        f'<form id="library-import" method="post" action="/ui/library" enctype="multipart/form-data">'
        f'<input type="hidden" name="csrf" value="{csrf}">'
        f'<input type="hidden" name="library" value="{html.escape(library_id, quote=True)}">'
        f'<input type="hidden" name="op" value="import">'
        f'<input type="hidden" name="source" value="picker">'
        '<label>File <input name="file" type="file"></label>'
        '<button type="submit">Import</button></form>'
        + '<div id="library-grid" class="lib-grid">'
        + ("".join(cards) or "<p>No items.</p>")
        + "</div>"
    )
    body = notice + '<div class="library">' + left + "<div>" + grid + "</div>" + '<aside class="lib-inspector">' + inspector + "</aside></div>"
    body += f'<script src="{library_script_path()}"></script>'
    return _page(session, "Library", body, wide=True)


def library_post(plane, session: dict, body: dict, peer_ip: str) -> Page:
    account_id = session["account"]["id"]
    op = str(body.get("op") or "")
    library_id = str(body.get("library") or "")
    if op == "create-library":
        created = create_library(plane, account_id, str(body.get("name") or ""), peer_ip)
        return Page(200, library_page(plane, session, {"library": created["id"]}, "<p>Library created.</p>"))
    chosen = library_or_default(plane, account_id, library_id)
    library_id = chosen["id"]
    if op == "import":
        raw, filename, source = _upload(body)
        saved = import_bytes(plane, account_id, library_id, raw, name=filename, source=source, peer_ip=peer_ip)
        return Page(200, library_page(plane, session, {"library": library_id, "item": saved["id"]}, "<p>Saved the file.</p>"))
    if op == "folder":
        add_folder(plane, account_id, library_id, str(body.get("name") or ""), peer_ip)
    elif op == "tag":
        add_tag(plane, account_id, library_id, str(body.get("name") or ""), peer_ip)
    elif op == "assign":
        update_item(plane, account_id, library_id, str(body.get("item") or ""), peer_ip=peer_ip, folder_id=str(body.get("folder") or ""))
    elif op == "tag-item":
        update_item(plane, account_id, library_id, str(body.get("item") or ""), peer_ip=peer_ip, tag=str(body.get("tag") or ""))
    elif op == "star":
        try:
            star = int(body.get("star") or 0)
        except (TypeError, ValueError) as exc:
            raise SsiosError("library_rejected", say(_language(plane, account_id), "library_rejected"), 400) from exc
        update_item(plane, account_id, library_id, str(body.get("item") or ""), peer_ip=peer_ip, star=star)
    elif op == "trash":
        update_item(plane, account_id, library_id, str(body.get("item") or ""), peer_ip=peer_ip, trash=True)
    elif op == "restore":
        update_item(plane, account_id, library_id, str(body.get("item") or ""), peer_ip=peer_ip, restore=True)
    elif op == "purge":
        purge_item(plane, account_id, library_id, str(body.get("item") or ""), str(body.get("confirm") or ""), peer_ip)
    else:
        raise SsiosError("library_rejected", say(_language(plane, account_id), "library_rejected"), 400)
    return Page(200, library_page(plane, session, {"library": library_id, "item": str(body.get("item") or "")}))


def library_raw(plane, session: dict, query: dict) -> Page:
    account_id = session["account"]["id"]
    chosen = library_or_default(plane, account_id, str(query.get("library") or ""))
    item = get_item(plane, account_id, chosen["id"], str(query.get("item") or ""))
    data, content_type = item_bytes(plane, account_id, chosen["id"], item["id"])
    show_bytes = item.get("ext") in {"png", "jpg", "gif", "webp", "bmp", "pdf"}
    if not show_bytes:
        data = solid_png(96, 120, (230, 230, 230))
        content_type = "image/png"
    return Page(200, data, content_type=content_type)


def _upload(body: dict) -> tuple[bytes, str, str]:
    source = str(body.get("source") or "picker")
    uploads = body.get("uploads") if isinstance(body.get("uploads"), list) else []
    if uploads and isinstance(uploads[0], dict) and isinstance(uploads[0].get("bytes"), (bytes, bytearray)):
        return bytes(uploads[0]["bytes"]), str(uploads[0].get("name") or "upload"), source if source in {"paste", "drag", "picker"} else "picker"
    return b"", str(body.get("name") or ""), source


def _language(plane, account_id: str) -> str:
    try:
        lang = plane.skills.account_prefs(account_id).get("language")
    except Exception:
        lang = "en"
    return lang if lang in {"en", "zh"} else "en"


