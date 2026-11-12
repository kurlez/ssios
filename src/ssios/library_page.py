"""Three-pane personal library. Sharing and smart folders are later milestones."""

from __future__ import annotations

import html

from ssios.errors import SsiosError
from ssios.library import (
    add_folder,
    add_tag,
    create_library,
    drop_summary,
    get_item,
    import_bytes,
    import_relative,
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
  var app = document.getElementById("library-app");
  var form = document.getElementById("library-import");
  if (!app || !form || app.getAttribute("data-bound") === "1") return;
  app.setAttribute("data-bound", "1");
  var csrf = app.getAttribute("data-csrf") || "";
  var libraryId = app.getAttribute("data-library") || "";
  var overlay = document.getElementById("drop-overlay");
  var log = document.getElementById("drop-log");
  var summary = document.getElementById("drop-summary");
  var applyChoice = "";
  var depth = 0;
  var hot = null;
  var generation = 0;
  function hasFiles(event) {
    var types = event.dataTransfer && event.dataTransfer.types;
    if (!types) return false;
    for (var i = 0; i < types.length; i += 1) if (types[i] === "Files") return true;
    return false;
  }
  function oneEntry(entry) {
    return new Promise(function (resolve) {
      if (!entry) { resolve([]); return; }
      if (entry.isDirectory) {
        resolve([{path: entry.name || "", directory: true}]);
        return;
      }
      if (entry.isFile) {
        entry.file(function (file) { resolve([{file: file, path: file.name || entry.name}]); });
        return;
      }
      resolve([]);
    });
  }
  function fromTransfer(dataTransfer) {
    var items = dataTransfer && dataTransfer.items ? Array.prototype.slice.call(dataTransfer.items) : [];
    var entries = [];
    items.forEach(function (item) {
      var entry = item.webkitGetAsEntry && item.webkitGetAsEntry();
      if (entry) entries.push(entry);
    });
    if (entries.length) {
      return Promise.all(entries.map(oneEntry)).then(function (groups) {
        var flat = [];
        groups.forEach(function (group) { flat = flat.concat(group); });
        return flat;
      });
    }
    var files = dataTransfer && dataTransfer.files ? Array.prototype.slice.call(dataTransfer.files) : [];
    return Promise.resolve(files.map(function (file) { return {file: file, path: file.webkitRelativePath || file.name}; }));
  }
  function uploadOne(row, folderId) {
    var line = document.createElement("li");
    line.textContent = row.path + " — 0%";
    log.appendChild(line);
    if (row.directory) {
      var reason = app.getAttribute("data-directory") || "";
      line.textContent = row.path + " — " + reason;
      line.className = "bad";
      return Promise.resolve({path: row.path, status: "rejected", reason: reason});
    }
    return new Promise(function (resolve) {
      var data = new FormData();
      data.set("csrf", csrf);
      data.set("library", libraryId);
      data.set("folder", folderId || "");
      data.set("relative", row.path);
      data.set("source", "drag");
      data.set("file", row.file, row.file.name || "file");
      var xhr = new XMLHttpRequest();
      xhr.open("POST", "/ui/library/drop");
      xhr.setRequestHeader("Accept", "application/json");
      xhr.upload.onprogress = function (event) {
        if (event.lengthComputable) line.textContent = row.path + " — " + Math.round(event.loaded * 100 / event.total) + "%";
      };
      xhr.onload = function () {
        var payload = {};
        try { payload = JSON.parse(xhr.responseText || "{}"); } catch (err) { payload = {}; }
        var result = (payload.results && payload.results[0]) || {path: row.path, status: "rejected", reason: xhr.statusText || "rejected"};
        if (result.status === "conflict") {
          if (applyChoice) {
            postDecision(result, applyChoice, line).then(resolve);
            return;
          }
          askConflict(result, line).then(resolve);
          return;
        }
        line.textContent = result.path + " — " + (result.reason || result.status);
        line.className = result.status === "rejected" ? "bad" : "ok";
        resolve(result);
      };
      xhr.onerror = function () {
        line.textContent = row.path + " — rejected";
        line.className = "bad";
        resolve({path: row.path, status: "rejected", reason: "rejected"});
      };
      xhr.send(data);
    });
  }
  function postDecision(result, decision, line) {
    return new Promise(function (resolve) {
      var data = new FormData();
      data.set("csrf", csrf);
      data.set("library", libraryId);
      data.set("conflict", result.conflict_id || "");
      data.set("decision", decision);
      data.set("relative", result.path || "");
      var xhr = new XMLHttpRequest();
      xhr.open("POST", "/ui/library/drop");
      xhr.setRequestHeader("Accept", "application/json");
      xhr.onload = function () {
        var payload = {};
        try { payload = JSON.parse(xhr.responseText || "{}"); } catch (err) { payload = {}; }
        var decided = (payload.results && payload.results[0]) || {path: result.path, status: "rejected", reason: "rejected"};
        line.textContent = (decided.path || result.path) + " — " + (decided.reason || decided.status);
        line.className = decided.status === "version" || decided.status === "kept" ? "ok" : "bad";
        resolve(decided);
      };
      xhr.send(data);
    });
  }
  function askConflict(result, line) {
    return new Promise(function (resolve) {
      line.textContent = (result.path || "") + " — " + (result.reason || "");
      line.className = "ask";
      var keep = document.createElement("button");
      keep.type = "button";
      keep.textContent = app.getAttribute("data-keep") || "Keep existing";
      if (result.default === "keep") keep.className = "primary";
      var replace = document.createElement("button");
      replace.type = "button";
      replace.textContent = app.getAttribute("data-replace") || "Replace with new";
      var apply = document.createElement("label");
      var box = document.createElement("input");
      box.type = "checkbox";
      apply.appendChild(box);
      apply.appendChild(document.createTextNode(app.getAttribute("data-apply") || "Apply to all remaining conflicts"));
      function choose(decision) {
        if (box.checked) applyChoice = decision;
        keep.disabled = true;
        replace.disabled = true;
        postDecision(result, decision, line).then(resolve);
      }
      keep.addEventListener("click", function () { choose("keep"); });
      replace.addEventListener("click", function () { choose("replace"); });
      line.appendChild(document.createTextNode(" "));
      line.appendChild(keep);
      line.appendChild(replace);
      line.appendChild(apply);
    });
  }
  function finish(results) {
    var token = generation;
    var saved = 0, versions = 0, skipped = 0;
    results.forEach(function (row) {
      if (row.status === "saved") saved += 1;
      else if (row.status === "version") versions += 1;
      else skipped += 1;
    });
    var text = app.getAttribute("data-summary") || "";
    summary.textContent = text.replace("{saved}", saved).replace("{versions}", versions).replace("{skipped}", skipped);
    if (saved || versions) setTimeout(function () {
      if (token === generation) location.reload();
    }, 4000);
  }
  function receive(dataTransfer, folderId) {
    var token = ++generation;
    fromTransfer(dataTransfer).then(function (rows) {
      var chain = Promise.resolve([]);
      rows.forEach(function (row) {
        chain = chain.then(function (results) {
          return uploadOne(row, folderId).then(function (result) { results.push(result); return results; });
        });
      });
      return chain.then(function (results) {
        if (token === generation) finish(results);
      });
    });
  }
  document.addEventListener("dragenter", function (event) {
    if (!hasFiles(event)) return;
    event.preventDefault();
    depth += 1;
    overlay.classList.add("show");
  });
  document.addEventListener("dragover", function (event) {
    if (!hasFiles(event)) return;
    event.preventDefault();
    var under = document.elementFromPoint(event.clientX, event.clientY);
    var folder = under && under.closest ? under.closest(".lib-folder") : null;
    if (hot && hot !== folder) hot.classList.remove("drop-hot");
    hot = folder;
    if (hot) hot.classList.add("drop-hot");
  });
  document.addEventListener("dragleave", function (event) {
    if (!hasFiles(event)) return;
    depth -= 1;
    if (depth <= 0) { depth = 0; overlay.classList.remove("show"); }
  });
  document.addEventListener("drop", function (event) {
    if (!hasFiles(event)) return;
    event.preventDefault();
    depth = 0;
    overlay.classList.remove("show");
    var folderId = hot ? hot.getAttribute("data-folder") || "" : "";
    if (hot) hot.classList.remove("drop-hot");
    hot = null;
    receive(event.dataTransfer, folderId);
  });
  document.addEventListener("paste", function (event) {
    var files = event.clipboardData && event.clipboardData.files;
    if (!files || !files.length) return;
    var rows = [];
    for (var i = 0; i < files.length; i += 1) rows.push({file: files[i], path: files[i].name});
    var chain = Promise.resolve([]);
    rows.forEach(function (row) {
      chain = chain.then(function (results) {
        return uploadOne(row, "").then(function (result) { results.push(result); return results; });
      });
    });
    chain.then(finish);
  });
  var picker = form.querySelector("input[type=file]");
  form.addEventListener("submit", function (event) {
    if (!picker || !picker.files || !picker.files.length) return;
    event.preventDefault();
    var rows = [];
    for (var i = 0; i < picker.files.length; i += 1) rows.push({file: picker.files[i], path: picker.files[i].name});
    var chain = Promise.resolve([]);
    rows.forEach(function (row) {
      chain = chain.then(function (results) {
        return uploadOne(row, "").then(function (result) { results.push(result); return results; });
      });
    });
    chain.then(finish);
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
    smart_id = str(query.get("smart") or "")
    text_query = " ".join(str(query.get("q") or "").split())[:200]
    if smart_id:
        from ssios.library_query import smart_items

        items = smart_items(plane, account_id, library_id, smart_id)
    else:
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
    if text_query:
        from ssios.library_query import _blob

        needle = text_query.casefold()
        items = [item for item in items if needle in _blob(item)]
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
            f'{_library_badge(row, lang)}<span>{html.escape(row["name"])}</span></a>'
        )
    folders = []
    known = [row for row in meta.get("folders") or [] if isinstance(row, dict)]
    for row in known:
        folders.append(
            f'<a class="lib-folder" data-folder="{html.escape(str(row.get("id") or ""), quote=True)}" '
            f'href="{base}&amp;folder={html.escape(str(row.get("id") or ""), quote=True)}">{html.escape(_folder_label(known, row))}</a>'
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
        avatar = ""
        if chosen.get("kind") == "shared":
            avatar = _adder_avatar(plane, item)
        cards.append(
            f'<a class="lib-card" href="{base}&amp;item={ident}">{avatar}{thumb}<span>{label}</span><span class="type-badge">{ext}</span></a>'
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
            + _analysis_block(selected, lang, csrf, library_id, ident)
        )
        if selected.get("trashed"):
            inspector += (
                f"<form method=\"post\" action=\"/ui/library\"><input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
                f"<input type=\"hidden\" name=\"library\" value=\"{html.escape(library_id, quote=True)}\">"
                f"<input type=\"hidden\" name=\"item\" value=\"{ident}\">"
                "<button name=\"op\" value=\"restore\" type=\"submit\">Restore</button></form>"
            )
            if chosen.get("role") == "owner":
                inspector += (
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
        '<div class="lib-side"><h2>' + html.escape(say(lang, "library_link")) + "</h2>"
        + "".join(switcher)
        + f'<form method="post" action="/ui/library"><input type="hidden" name="csrf" value="{csrf}">'
        + '<label>Name <input name="name" maxlength="40"></label>'
        + '<button name="op" value="create-library" type="submit">Create</button></form>'
        + f'<p><a href="{base}">All</a> <a href="{base}&amp;uncategorized=1">Uncategorized</a> '
        + f'<a href="{base}&amp;untagged=1">Untagged</a> <a href="{base}&amp;trash=1">Trash</a></p>'
        + "<h2>Folders</h2>" + ("".join(f"<p>{row}</p>" for row in folders) or "<p>No folders.</p>")
        + f'<form method="post" action="/ui/library"><input type="hidden" name="csrf" value="{csrf}">'
        + f'<input type="hidden" name="library" value="{html.escape(library_id, quote=True)}">'
        + '<label>Folder <input name="name" maxlength="40"></label>'
        + '<button name="op" value="folder" type="submit">Add folder</button></form>'
        + "<h2>" + html.escape(say(lang, "smart_folder")) + "</h2>"
        + _smart_block(meta, base, lang, csrf, library_id)
        + "<h2>" + html.escape(say(lang, "watch_folder")) + "</h2>"
        + _watch_form(meta, lang, csrf, library_id)
        + "<h2>Tags</h2>" + ("".join(f"<p>{row}</p>" for row in tags) or "<p>No tags.</p>")
        + f'<form method="post" action="/ui/library"><input type="hidden" name="csrf" value="{csrf}">'
        + f'<input type="hidden" name="library" value="{html.escape(library_id, quote=True)}">'
        + '<label>Tag <input name="name" maxlength="40"></label>'
        + '<button name="op" value="tag" type="submit">Add tag</button></form>'
        + _share_block(chosen, lang, csrf, library_id)
        + "</div>"
    )
    grid = (
        f'<form id="library-import" method="post" action="/ui/library" enctype="multipart/form-data">'
        f'<input type="hidden" name="csrf" value="{csrf}">'
        f'<input type="hidden" name="library" value="{html.escape(library_id, quote=True)}">'
        f'<input type="hidden" name="op" value="import">'
        f'<input type="hidden" name="source" value="picker">'
        '<label>File <input name="file" type="file" multiple></label>'
        '<button type="submit">Import</button></form>'
        + '<div id="library-grid" class="lib-grid">'
        + ("".join(cards) or "<p>No items.</p>")
        + "</div>"
    )
    body = (
        notice
        + f'<div class="library" id="library-app" data-library="{html.escape(library_id, quote=True)}" data-csrf="{csrf}" '
        + f'data-overlay="{html.escape(say(lang, "drop_overlay"), quote=True)}" '
        + f'data-directory="{html.escape(say(lang, "drop_directory"), quote=True)}" '
        + f'data-keep="{html.escape(say(lang, "conflict_keep"), quote=True)}" '
        + f'data-replace="{html.escape(say(lang, "conflict_replace"), quote=True)}" '
        + f'data-apply="{html.escape(say(lang, "conflict_apply"), quote=True)}" '
        + f'data-summary="{html.escape(say(lang, "drop_summary", saved="{saved}", versions="{versions}", skipped="{skipped}"), quote=True)}">'
        + left
        + "<div>"
        + f'<form class="file-search" method="get" action="/ui/library">'
        + f'<input type="hidden" name="library" value="{html.escape(library_id, quote=True)}">'
        + f'<label>{html.escape(say(lang, "search_files"))} <input name="q" value="{html.escape(text_query, quote=True)}" maxlength="200"></label>'
        + f'<button type="submit">{html.escape(say(lang, "search_files"))}</button></form>'
        + '<ol id="drop-log" class="drop-log"></ol><p id="drop-summary"></p>'
        + grid
        + "</div>"
        + '<aside class="lib-inspector">'
        + inspector
        + "</aside>"
        + f'<div id="drop-overlay" class="drop-overlay">{html.escape(say(lang, "drop_overlay"))}</div></div>'
    )
    body += f'<script src="{library_script_path()}"></script>'
    return _page(session, say(lang, "library_link"), body, wide=True)


def library_post(plane, session: dict, body: dict, peer_ip: str) -> Page:
    account_id = session["account"]["id"]
    op = str(body.get("op") or "")
    library_id = str(body.get("library") or "")
    if op == "create-library":
        created = create_library(plane, account_id, str(body.get("name") or ""), peer_ip)
        return Page(200, library_page(plane, session, {"library": created["id"]}, "<p>Created.</p>"))
    chosen = library_or_default(plane, account_id, library_id)
    library_id = chosen["id"]
    if op == "resolve":
        from ssios.library import resolve_conflict

        decided = resolve_conflict(plane, account_id, library_id, str(body.get("conflict") or ""), str(body.get("decision") or ""), peer_ip)
        return Page(200, library_page(plane, session, {"library": library_id, "item": decided.get("id") or ""}, f"<p>{html.escape(decided.get('reason') or '')}</p>"))
    if op == "import":
        raw, filename, source = _upload(body)
        saved = import_bytes(plane, account_id, library_id, raw, name=filename, source=source, peer_ip=peer_ip)
        if saved.get("result") == "conflict":
            return Page(200, library_page(plane, session, {"library": library_id}, _conflict_form(session, library_id, saved, _language(plane, account_id))))
        return Page(200, library_page(plane, session, {"library": library_id, "item": saved["id"]}, "<p>Saved the file.</p>"))
    if op == "smart":
        from ssios.library_query import add_smart_folder

        add_smart_folder(plane, account_id, library_id, str(body.get("name") or ""), str(body.get("query") or ""), peer_ip)
        return Page(200, library_page(plane, session, {"library": library_id}, f"<p>{html.escape(say(_language(plane, account_id), 'smart_saved'))}</p>"))
    if op == "watch":
        from ssios.library_watch import scan_watch, set_watch

        set_watch(
            plane,
            account_id,
            library_id,
            str(body.get("path") or ""),
            str(body.get("enabled") or "") == "1",
            peer_ip,
            delete_source=str(body.get("delete_source") or "") == "1",
        )
        found = scan_watch(plane, account_id, library_id, peer_ip)
        note = say(_language(plane, account_id), "watch_saved")
        if found.get("imported"):
            note += " " + say(_language(plane, account_id), "drop_saved")
        return Page(200, library_page(plane, session, {"library": library_id}, f"<p>{html.escape(note)}</p>"))
    if op == "analyze":
        from ssios.library_analyze import analyze_item

        analyzed = analyze_item(plane, account_id, library_id, str(body.get("item") or ""), peer_ip)
        return Page(200, library_page(plane, session, {"library": library_id, "item": analyzed["id"]}, f"<p>{html.escape(say(_language(plane, account_id), 'analyze_saved'))}</p>"))
    if op == "remember":
        from ssios.library_analyze import stage_core_memory

        stage_core_memory(plane, account_id, library_id, str(body.get("item") or ""), peer_ip)
        return Page(200, library_page(plane, session, {"library": library_id, "item": str(body.get("item") or "")}, f"<p>{html.escape(say(_language(plane, account_id), 'memory_pending'))}</p>"))
    if op == "share":
        from ssios.library_share import share_library

        share_library(plane, account_id, library_id, str(body.get("username") or ""), str(body.get("role") or "view"), peer_ip)
        return Page(200, library_page(plane, session, {"library": library_id}, "<p>Shared.</p>"))
    if op == "unshare":
        from ssios.library_share import unshare_library

        unshare_library(plane, account_id, library_id, str(body.get("username") or ""), peer_ip)
        return Page(200, library_page(plane, session, {"library": library_id}, "<p>Removed.</p>"))
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


def _library_badge(row: dict, lang: str) -> str:
    if row.get("kind") != "shared":
        return f'{icon("person")}<span class="lib-badge">{html.escape(say(lang, "library_personal"))}</span>'
    if row.get("role") == "owner":
        avatars = "".join(
            f'<span class="lib-mini" title="{html.escape(str(member.get("username") or ""), quote=True)}">{html.escape(_initial(str(member.get("username") or "")))}</span>'
            for member in row.get("members") or []
            if isinstance(member, dict)
        )
        return f'{icon("people")}{avatars}<span class="lib-badge">{html.escape(say(lang, "library_shared"))}</span>'
    label = say(lang, "library_shared_by", name=str(row.get("owner_name") or ""))
    return f'{icon("people")}<span class="lib-badge">{html.escape(label)}</span>'


def _adder_avatar(plane, item: dict) -> str:
    from ssios.library import _username

    name = _username(plane, str(item.get("added_by") or ""))
    if not name:
        return ""
    return f'<span class="lib-avatar" title="{html.escape(name, quote=True)}">{html.escape(_initial(name))}</span>'


def _initial(name: str) -> str:
    return (name[:1] or "?").upper()


def _share_block(chosen: dict, lang: str, csrf: str, library_id: str) -> str:
    if chosen.get("role") != "owner":
        return ""
    options = "".join(
        f'<option value="{key}">{html.escape(say(lang, label))}</option>'
        for key, label in (("view", "share_view"), ("add", "share_add"), ("organize", "share_organize"))
    )
    members = []
    for member in chosen.get("members") or []:
        if not isinstance(member, dict) or not member.get("username"):
            continue
        members.append(
            f'<form method="post" action="/ui/library"><input type="hidden" name="csrf" value="{csrf}">'
            f'<input type="hidden" name="library" value="{html.escape(library_id, quote=True)}">'
            f'<input type="hidden" name="username" value="{html.escape(str(member.get("username") or ""), quote=True)}">'
            f'<span>{html.escape(str(member.get("username") or ""))}</span> '
            f'<button name="op" value="unshare" type="submit">{html.escape(say(lang, "share_remove"))}</button></form>'
        )
    return (
        "<h2>" + html.escape(say(lang, "library_shared")) + "</h2>"
        + "".join(members)
        + f'<form method="post" action="/ui/library"><input type="hidden" name="csrf" value="{csrf}">'
        + f'<input type="hidden" name="library" value="{html.escape(library_id, quote=True)}">'
        + f'<label>{html.escape(say(lang, "share_with"))} <input name="username" maxlength="32"></label>'
        + f'<label><select name="role">{options}</select></label>'
        + f'<button name="op" value="share" type="submit">{html.escape(say(lang, "library_shared"))}</button></form>'
    )


def _analysis_block(selected: dict, lang: str, csrf: str, library_id: str, ident: str) -> str:
    description = str(selected.get("description") or "")
    ocr = str(selected.get("ocr") or "")
    shown = ""
    if description:
        shown += f"<p>{html.escape(description)}</p>"
    if ocr:
        shown += f"<p>{html.escape(say(lang, 'ocr_label'))}: {html.escape(ocr)}</p>"
    forms = (
        f'<form method="post" action="/ui/library"><input type="hidden" name="csrf" value="{csrf}">'
        f'<input type="hidden" name="library" value="{html.escape(library_id, quote=True)}">'
        f'<input type="hidden" name="item" value="{ident}">'
        f'<button name="op" value="analyze" type="submit">{html.escape(say(lang, "analyze"))}</button></form>'
    )
    if description:
        forms += (
            f'<form method="post" action="/ui/library"><input type="hidden" name="csrf" value="{csrf}">'
            f'<input type="hidden" name="library" value="{html.escape(library_id, quote=True)}">'
            f'<input type="hidden" name="item" value="{ident}">'
            f'<button name="op" value="remember" type="submit">{html.escape(say(lang, "memory_core"))}</button></form>'
        )
    return shown + forms


def _smart_block(meta: dict, base: str, lang: str, csrf: str, library_id: str) -> str:
    links = []
    for row in meta.get("smart_folders") or []:
        if not isinstance(row, dict):
            continue
        links.append(
            f'<p><a href="{base}&amp;smart={html.escape(str(row.get("id") or ""), quote=True)}">'
            f'{html.escape(str(row.get("name") or ""))}</a></p>'
        )
    form = (
        f'<form method="post" action="/ui/library"><input type="hidden" name="csrf" value="{csrf}">'
        f'<input type="hidden" name="library" value="{html.escape(library_id, quote=True)}">'
        f'<label>{html.escape(say(lang, "smart_folder"))} <input name="name" maxlength="40"></label>'
        f'<label>{html.escape(say(lang, "smart_query"))} <input name="query" maxlength="200"></label>'
        f'<button name="op" value="smart" type="submit">{html.escape(say(lang, "smart_add"))}</button></form>'
    )
    return "".join(links) + form


def _watch_form(meta: dict, lang: str, csrf: str, library_id: str) -> str:
    watch = meta.get("watch") if isinstance(meta.get("watch"), dict) else {}
    enabled = " checked" if watch.get("enabled") else ""
    delete = " checked" if watch.get("delete_source") else ""
    path = html.escape(str(watch.get("path") or ""), quote=True)
    return (
        f'<form method="post" action="/ui/library"><input type="hidden" name="csrf" value="{csrf}">'
        f'<input type="hidden" name="library" value="{html.escape(library_id, quote=True)}">'
        f'<p>{html.escape(say(lang, "watch_hint"))}</p>'
        f'<label>{html.escape(say(lang, "watch_folder"))} <input name="path" value="{path}"></label>'
        f'<label><input type="checkbox" name="enabled" value="1"{enabled}> {html.escape(say(lang, "watch_enable"))}</label>'
        f'<label><input type="checkbox" name="delete_source" value="1"{delete}> {html.escape(say(lang, "watch_delete"))}</label>'
        f'<button name="op" value="watch" type="submit">{html.escape(say(lang, "watch_saved"))}</button></form>'
    )


def _folder_label(folders: list[dict], row: dict) -> str:
    names = []
    current = row
    seen: set[str] = set()
    while isinstance(current, dict) and str(current.get("id") or "") not in seen:
        seen.add(str(current.get("id") or ""))
        names.append(str(current.get("name") or ""))
        parent = str(current.get("parent_id") or "")
        current = next((item for item in folders if str(item.get("id") or "") == parent), None)
    return " / ".join(reversed([name for name in names if name]))


def _conflict_form(session: dict, library_id: str, saved: dict, lang: str) -> str:
    csrf = html.escape(str(session.get("csrf") or ""), quote=True)
    conflict_id = html.escape(str(saved.get("conflict_id") or ""), quote=True)
    primary = "primary" if saved.get("default") == "keep" else ""
    return (
        f"<p class=\"conflict\">{html.escape(str(saved.get('reason') or ''))}</p>"
        f"<form class=\"conflict-choice\" method=\"post\" action=\"/ui/library\">"
        f"<input type=\"hidden\" name=\"csrf\" value=\"{csrf}\">"
        f"<input type=\"hidden\" name=\"op\" value=\"resolve\">"
        f"<input type=\"hidden\" name=\"library\" value=\"{html.escape(library_id, quote=True)}\">"
        f"<input type=\"hidden\" name=\"conflict\" value=\"{conflict_id}\">"
        f"<button class=\"{primary}\" name=\"decision\" value=\"keep\" type=\"submit\">{html.escape(say(lang, 'conflict_keep'))}</button>"
        f"<button name=\"decision\" value=\"replace\" type=\"submit\">{html.escape(say(lang, 'conflict_replace'))}</button></form>"
    )


def accept_drop(plane, session: dict, fields: dict, files: list[dict], peer_ip: str) -> dict:
    from ssios.library import MEDIA_LIMIT, resolve_conflict

    account_id = session["account"]["id"]
    lang = _language(plane, account_id)
    chosen = library_or_default(plane, account_id, str(fields.get("library") or ""))
    if fields.get("decision") and fields.get("conflict"):
        decided = resolve_conflict(plane, account_id, chosen["id"], str(fields.get("conflict") or ""), str(fields.get("decision") or ""), peer_ip)
        decided["path"] = fields.get("relative") or decided.get("path") or ""
        results = [decided]
        return {"results": results, "summary": drop_summary(lang, results)}
    folder_id = str(fields.get("folder") or "")
    source = str(fields.get("source") or "drag")
    relative = str(fields.get("relative") or "")
    results = []
    if not files:
        results.append(
            {
                "path": relative,
                "status": "rejected",
                "code": "library_empty",
                "reason": say(lang, "library_empty"),
                "id": "",
            }
        )
    for item in files:
        shown = relative or str(item.get("name") or "")
        if item.get("too_big"):
            results.append(
                {
                    "path": shown,
                    "status": "rejected",
                    "code": "library_too_large",
                    "reason": say(lang, "library_too_large", limit=MEDIA_LIMIT),
                    "id": "",
                }
            )
            continue
        results.append(
            import_relative(
                plane,
                account_id,
                chosen["id"],
                item.get("path"),
                relative=shown,
                source=source if source in {"paste", "drag", "picker"} else "drag",
                peer_ip=peer_ip,
                folder_id=folder_id,
            )
        )
    return {"results": results, "summary": drop_summary(lang, results)}


def _language(plane, account_id: str) -> str:
    try:
        lang = plane.skills.account_prefs(account_id).get("language")
    except Exception:
        lang = "en"
    return lang if lang in {"en", "zh"} else "en"


