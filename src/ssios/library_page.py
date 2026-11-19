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
  // A dropped folder is read with its subfolders. Hidden files are skipped, and a drop stops at MAX_FILES files and MAX_DEPTH levels.
  var MAX_FILES = 5000, MAX_DEPTH = 12, counted = 0;
  function readAll(reader) {
    return new Promise(function (resolve) {
      var all = [];
      (function next() {
        reader.readEntries(function (batch) {
          if (!batch.length) { resolve(all); return; }
          all = all.concat(Array.prototype.slice.call(batch));
          next();
        }, function () { resolve(all); });
      })();
    });
  }
  function oneEntry(entry, prefix, level) {
    prefix = prefix || ""; level = level || 0;
    return new Promise(function (resolve) {
      if (!entry || (entry.name || "").charAt(0) === ".") { resolve([]); return; }
      var path = prefix + (entry.name || "");
      if (entry.isDirectory) {
        if (level >= MAX_DEPTH) { resolve([{path: path, tooDeep: true}]); return; }
        readAll(entry.createReader()).then(function (children) {
          return Promise.all(children.map(function (child) { return oneEntry(child, path + "/", level + 1); }));
        }).then(function (groups) {
          var flat = [];
          groups.forEach(function (group) { flat = flat.concat(group); });
          resolve(flat);
        });
        return;
      }
      if (entry.isFile) {
        if (counted >= MAX_FILES) { resolve([{path: path, tooMany: true}]); return; }
        counted += 1;
        entry.file(function (file) { resolve([{file: file, path: path}]); }, function () { resolve([]); });
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
    counted = 0;
    if (entries.length) {
      return Promise.all(entries.map(function (entry) { return oneEntry(entry, "", 0); })).then(function (groups) {
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
    if (row.tooDeep || row.tooMany) {
      var reason = app.getAttribute(row.tooDeep ? "data-too-deep" : "data-too-many") || "";
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
      data.set("source", row.source || "drag");
      var tagBox = document.getElementById("lib-import-tags");
      if (tagBox && tagBox.value.trim()) data.set("tags", tagBox.value);
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
    for (var i = 0; i < files.length; i += 1) rows.push({file: files[i], path: files[i].name, source: "paste"});
    var chain = Promise.resolve([]);
    rows.forEach(function (row) {
      chain = chain.then(function (results) {
        return uploadOne(row, app.getAttribute("data-folder") || "").then(function (result) { results.push(result); return results; });
      });
    });
    chain.then(finish);
  });
  var picker = form.querySelector("input[name=file]");
  if (picker) picker.addEventListener("change", function () {
    if (picker.files && picker.files.length) form.requestSubmit();
  });
  var folderPicker = form.querySelector("input[name=folder_files]");
  if (folderPicker) folderPicker.addEventListener("change", function () {
    if (!folderPicker.files || !folderPicker.files.length) return;
    picker = folderPicker;
    form.requestSubmit();
  });
  form.addEventListener("submit", function (event) {
    if (!picker || !picker.files || !picker.files.length) return;
    event.preventDefault();
    var rows = [];
    for (var i = 0; i < picker.files.length; i += 1) {
      var chosenFile = picker.files[i], relative = chosenFile.webkitRelativePath || chosenFile.name;
      if (relative.split("/").some(function (part) { return part.charAt(0) === "."; })) continue;
      if (rows.length >= MAX_FILES) { rows.push({path: relative, tooMany: true}); break; }
      rows.push({file: chosenFile, path: relative, source: chosenFile.webkitRelativePath ? "folder" : "picker"});
    }
    var chain = Promise.resolve([]);
    rows.forEach(function (row) {
      chain = chain.then(function (results) {
        return uploadOne(row, app.getAttribute("data-folder") || "").then(function (result) { results.push(result); return results; });
      });
    });
    chain.then(finish);
  });
  document.querySelectorAll("img[data-tile]").forEach(function (img) {
    img.addEventListener("error", function () {
      var tile = document.createElement("span");
      tile.className = "type-tile modest";
      tile.textContent = img.getAttribute("data-tile") || "";
      img.replaceWith(tile);
    });
  });
  var heading = document.querySelector(".lib-rename");
  if (heading) {
    function beginRename() {
      if (heading.querySelector("input")) return;
      var full = heading.getAttribute("data-name") || "";
      var input = document.createElement("input");
      input.className = "lib-rename-input";
      input.value = full;
      input.setAttribute("maxlength", "120");
      var text = heading.querySelector(".lib-rename-text");
      if (text) text.replaceWith(input);
      var dot = full.lastIndexOf(".");
      input.focus();
      if (dot > 0) input.setSelectionRange(0, dot);
      var cancel = false;
      input.addEventListener("keydown", function (event) {
        if (event.key === "Escape") { cancel = true; input.blur(); }
        if (event.key === "Enter") { event.preventDefault(); input.blur(); }
      });
      input.addEventListener("blur", function () {
        if (cancel) { restore(full); return; }
        saveRename(input.value, full);
      });
    }
    function restore(full) {
      var input = heading.querySelector("input");
      if (!input) return;
      var text = document.createElement("span");
      text.className = "lib-rename-text";
      text.textContent = full;
      input.replaceWith(text);
      var error = document.querySelector(".lib-rename-error");
      if (error) { error.hidden = true; error.textContent = ""; }
    }
    function saveRename(value, previous) {
      var data = new URLSearchParams();
      data.set("csrf", csrf);
      data.set("library", libraryId);
      data.set("item", heading.getAttribute("data-id") || "");
      data.set("op", "rename");
      data.set("ajax", "1");
      data.set("name", value);
      fetch("/ui/library", {method: "POST", body: data, headers: {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}})
        .then(function (response) { return response.json(); })
        .then(function (payload) {
          if (!payload.ok) {
            var error = document.querySelector(".lib-rename-error");
            if (error) { error.hidden = false; error.textContent = payload.error || ""; }
            var input = heading.querySelector("input");
            if (input) input.focus();
            return;
          }
          heading.setAttribute("data-name", payload.name);
          restore(payload.name);
          var card = document.querySelector('.lib-card[data-item="' + (payload.id || "") + '"] .name');
          if (card) card.textContent = payload.name;
        })
        .catch(function () { restore(previous); });
    }
    heading.addEventListener("click", function (event) {
      var target = event.target;
      if (!target || !target.closest) return;
      if (target.closest(".lib-rename-text") || target.closest(".lib-rename-edit")) beginRename();
    });
  }
  bindSelection();
  var settings = document.getElementById("lib-settings");
  var openSettings = document.getElementById("lib-settings-open");
  if (settings && openSettings && settings.showModal) {
    openSettings.addEventListener("click", function () { settings.showModal(); });
  }
  function bindSelection() {
    var grid = document.getElementById("library-grid");
    var bar = document.getElementById("lib-batch");
    if (!grid || !bar) return;
    var selected = [];
    var anchor = -1;
    var role = app.getAttribute("data-role") || "owner";
    var canTag = role === "add" || role === "organize" || role === "owner";
    var canMove = role === "organize" || role === "owner";
    var canTrash = canTag;
    var canPurge = role === "owner";
    bar.querySelectorAll("[data-need]").forEach(function (button) {
      var need = button.getAttribute("data-need");
      var allowed = need === "view" || (need === "tag" && canTag) || (need === "move" && canMove) || (need === "trash" && canTrash) || (need === "purge" && canPurge);
      if (!allowed) button.hidden = true;
    });
    function cards() { return Array.prototype.slice.call(grid.querySelectorAll(".lib-card")); }
    function paint() {
      var set = {};
      selected.forEach(function (id) { set[id] = true; });
      cards().forEach(function (card) {
        var on = !!set[card.getAttribute("data-item")];
        card.classList.toggle("selected", on);
        var box = card.querySelector(".lib-check");
        if (box) box.checked = on;
      });
      app.classList.toggle("selecting", selected.length > 0);
      if (selected.length >= 2) app.classList.add("has-selection");
      else if (!document.getElementById("lib-single")) app.classList.remove("has-selection");
      var multi = document.getElementById("lib-multi");
      var single = document.getElementById("lib-single");
      if (selected.length >= 2) {
        bar.hidden = false;
        var count = bar.querySelector("[data-count]");
        if (count) count.textContent = (app.getAttribute("data-selected") || "").replace("{count}", String(selected.length));
        fillMulti(multi);
        if (multi) multi.hidden = false;
        if (single) single.hidden = true;
      } else {
        bar.hidden = true;
        if (multi) multi.hidden = true;
        if (single) single.hidden = false;
      }
    }
    function fillMulti(multi) {
      if (!multi) return;
      var total = 0;
      var common = null;
      selected.forEach(function (id) {
        var card = grid.querySelector('.lib-card[data-item="' + id + '"]');
        if (!card) return;
        total += parseInt(card.getAttribute("data-size") || "0", 10) || 0;
        var tags = (card.getAttribute("data-tags") || "").split("|").filter(Boolean);
        common = common === null ? tags : common.filter(function (tag) { return tags.indexOf(tag) >= 0; });
      });
      multi.querySelector("[data-count]").textContent = (app.getAttribute("data-selected") || "").replace("{count}", String(selected.length));
      multi.querySelector("[data-size]").textContent = (multi.getAttribute("data-total") || "").replace("{size}", humanSize(total));
      var box = multi.querySelector("[data-tags]");
      box.textContent = "";
      (common || []).forEach(function (tag) {
        var chip = document.createElement("span");
        chip.className = "lib-chip";
        chip.textContent = tag;
        box.appendChild(chip);
      });
      if (!common || !common.length) box.textContent = multi.getAttribute("data-none") || "";
    }
    function humanSize(size) {
      if (size < 1024) return size + " B";
      if (size < 1048576) return Math.round(size / 1024) + " KB";
      return (size / 1048576).toFixed(1) + " MB";
    }
    function choose(id, index, event) {
      var list = cards();
      if (event.shiftKey && anchor >= 0) {
        var start = Math.min(anchor, index);
        var end = Math.max(anchor, index);
        selected = list.slice(start, end + 1).map(function (card) { return card.getAttribute("data-item"); });
      } else if (event.metaKey || event.ctrlKey) {
        var at = selected.indexOf(id);
        if (at >= 0) selected.splice(at, 1);
        else selected.push(id);
        anchor = index;
      } else {
        selected = [id];
        anchor = index;
      }
      paint();
    }
    grid.addEventListener("click", function (event) {
      var card = event.target.closest && event.target.closest(".lib-card");
      if (!card) return;
      var box = event.target.closest(".lib-check");
      var modified = event.metaKey || event.ctrlKey || event.shiftKey || box;
      if (!modified && selected.length < 2) return;
      event.preventDefault();
      var list = cards();
      choose(card.getAttribute("data-item"), list.indexOf(card), box ? {metaKey: true, ctrlKey: true, shiftKey: false} : event);
    });
    grid.addEventListener("mousedown", function (event) {
      if (event.button !== 0 || (event.target.closest && event.target.closest(".lib-card"))) return;
      var band = document.createElement("div");
      band.className = "lib-marquee";
      document.body.appendChild(band);
      var x = event.clientX;
      var y = event.clientY;
      function move(ev) {
        var left = Math.min(x, ev.clientX);
        var top = Math.min(y, ev.clientY);
        band.style.left = left + "px";
        band.style.top = top + "px";
        band.style.width = Math.abs(ev.clientX - x) + "px";
        band.style.height = Math.abs(ev.clientY - y) + "px";
      }
      function up(ev) {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        var rect = band.getBoundingClientRect();
        band.remove();
        if (rect.width < 4 && rect.height < 4) return;
        selected = [];
        cards().forEach(function (card, index) {
          var box = card.getBoundingClientRect();
          var hit = !(box.right < rect.left || box.left > rect.right || box.bottom < rect.top || box.top > rect.bottom);
          if (hit) selected.push(card.getAttribute("data-item"));
          if (hit) anchor = index;
        });
        paint();
      }
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });
    document.addEventListener("keydown", function (event) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "a" && app.contains(document.activeElement)) {
        event.preventDefault();
        selected = cards().map(function (card) { return card.getAttribute("data-item"); });
        anchor = 0;
        paint();
      }
      if (event.key === "Escape") { selected = []; anchor = -1; paint(); }
    });
    cards().forEach(function (card) {
      card.addEventListener("dragstart", function (event) {
        var id = card.getAttribute("data-item");
        if (selected.indexOf(id) < 0) { selected = [id]; paint(); }
        event.dataTransfer.setData("text/plain", selected.join(","));
        event.dataTransfer.effectAllowed = "copy";
      });
    });
    document.addEventListener("drop", function (event) {
      var payload = event.dataTransfer && event.dataTransfer.getData("text/plain");
      if (!payload || payload.indexOf("item-") !== 0 || !hot) return;
      event.preventDefault();
      event.stopPropagation();
      postBatch("folder", payload, hot.getAttribute("data-folder") || "");
    }, true);
    function postBatch(action, ids, extra) {
      var data = new URLSearchParams();
      data.set("csrf", csrf);
      data.set("library", libraryId);
      data.set("op", "batch");
      data.set("ajax", "1");
      data.set("action", action);
      data.set("ids", typeof ids === "string" ? ids : selected.join(","));
      if (extra) data.set("folder", extra);
      return fetch("/ui/library", {method: "POST", body: data, headers: {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}})
        .then(function (response) { return response.json(); });
    }
    bar.addEventListener("click", function (event) {
      var button = event.target.closest && event.target.closest("[data-batch]");
      if (!button) return;
      var action = button.getAttribute("data-batch");
      if (action === "clear") { selected = []; anchor = -1; paint(); return; }
      if (action === "tag") { document.getElementById("lib-tag-dialog").showModal(); return; }
      if (action === "folder") { document.getElementById("lib-folder-dialog").showModal(); return; }
      if (action === "purge") { document.getElementById("lib-purge-dialog").showModal(); return; }
      if (action === "download") { downloadZip(); return; }
      postBatch(action, selected.join(","), app.getAttribute("data-folder") || "").then(function () { location.reload(); });
    });
    wireDialog("lib-tag-dialog", function (form) { return form.querySelector("[name=tag]").value; }, "tag");
    wireDialog("lib-folder-dialog", function (form) { return form.querySelector("[name=folder]").value; }, "folder");
    var purge = document.getElementById("lib-purge-dialog");
    if (purge) purge.addEventListener("submit", function (event) {
      event.preventDefault();
      var data = new URLSearchParams();
      data.set("csrf", csrf);
      data.set("library", libraryId);
      data.set("op", "batch");
      data.set("ajax", "1");
      data.set("action", "purge");
      data.set("ids", selected.join(","));
      data.set("confirm", purge.querySelector("[name=confirm]").value);
      fetch("/ui/library", {method: "POST", body: data, headers: {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}}).then(function () { location.reload(); });
      purge.close();
    });
    function wireDialog(id, value, kind) {
      var dialog = document.getElementById(id);
      if (!dialog) return;
      dialog.addEventListener("submit", function (event) {
        event.preventDefault();
        var submitter = event.submitter;
        var action = submitter && submitter.getAttribute("data-batch") || kind;
        var data = new URLSearchParams();
        data.set("csrf", csrf);
        data.set("library", libraryId);
        data.set("op", "batch");
        data.set("ajax", "1");
        data.set("action", action);
        data.set("ids", selected.join(","));
        if (kind === "tag") data.set("tag", value(dialog));
        if (kind === "folder") data.set("folder", value(dialog));
        fetch("/ui/library", {method: "POST", body: data, headers: {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}})
          .then(function () { location.reload(); });
      });
      dialog.querySelectorAll("[data-close]").forEach(function (button) {
        button.addEventListener("click", function () { dialog.close(); });
      });
    }
    function downloadZip() {
      var data = new URLSearchParams();
      data.set("csrf", csrf);
      data.set("library", libraryId);
      data.set("op", "download");
      data.set("ids", selected.join(","));
      fetch("/ui/library", {method: "POST", body: data, headers: {"Content-Type": "application/x-www-form-urlencoded"}})
        .then(function (response) { return response.blob(); })
        .then(function (blob) {
          var link = document.createElement("a");
          link.href = URL.createObjectURL(blob);
          link.download = "files.zip";
          link.click();
        });
    }
  }
})();
(function () {
  // A delete in the tag manager asks first. Without a script the form still posts.
  document.addEventListener("click", function (event) {
    var button = event.target.closest ? event.target.closest("button[data-confirm]") : null;
    if (button && !window.confirm(button.getAttribute("data-confirm"))) event.preventDefault();
  });
})();
(function () {
  // The filter bar sends only the filters that are set, so a filtered view is a short link.
  var form = document.getElementById("lib-filters");
  if (!form) return;
  form.addEventListener("submit", function () {
    Array.prototype.forEach.call(form.elements, function (field) {
      if (!field.name || field.type === "submit") return;
      var empty = field.value === "" || (field.name === "tagmode" && field.value === "all" && !form.elements.tags.value);
      if (field.name === "dir" || field.name === "sort") empty = false;
      if (empty) field.disabled = true;
    });
    setTimeout(function () {
      Array.prototype.forEach.call(form.elements, function (field) { field.disabled = false; });
    }, 0);
  });
})();
"""


def library_script_path() -> str:
    from ssios.ui import _asset_hash

    return "/ui/assets/library-" + _asset_hash(LIBRARY_JS) + ".js"


# Cards per page. Eagle scrolls without end; here the next cards are one link away, which keeps a page fast.
PAGE_SIZE = 400


def library_page(plane, session: dict, query: dict, notice: str = "") -> str:
    from ssios.ui import _page

    account_id = session["account"]["id"]
    lang = _language(plane, account_id)
    chosen = library_or_default(plane, account_id, str(query.get("library") or ""))
    library_id = chosen["id"]
    admin = str(session["account"].get("role") or "") == "admin"
    if chosen.get("offline"):
        # The disk is not connected. Show the other libraries and say where this one is. Nothing is created.
        links = "".join(
            f'<a class="lib-switch{" active" if row["id"] == library_id else ""}" href="/ui/library?library={html.escape(row["id"], quote=True)}">{_library_badge(row, lang)}<span>{html.escape(row["name"])}</span></a>'
            for row in list_libraries(plane, account_id)
        )
        body = (
            f'<div class="library"><div class="lib-side">{links}</div><div class="lib-main">'
            f'<p class="notice">{html.escape(say(lang, "library_offline"))}</p>'
            f'<p>{html.escape(say(lang, "lib_where"))}: <code>{html.escape(str(chosen.get("location") or ""))}</code></p></div></div>'
        )
        return _page(session, "Files", notice + body, bleed=True)
    meta = metadata(plane, account_id, library_id)
    folder = str(query.get("folder") or "")
    tag = str(query.get("tag") or "")
    trash = str(query.get("trash") or "") == "1"
    uncategorized = str(query.get("uncategorized") or "") == "1"
    untagged = str(query.get("untagged") or "") == "1"
    smart_id = str(query.get("smart") or "")
    text_query = " ".join(str(query.get("q") or "").split())[:200]
    from ssios.library import _items, filter_items

    # One catalog read for the whole page: the listing, the counts, and the tag list all come from it.
    catalog = _items(plane, account_id, library_id)
    from ssios.library_filter import apply as apply_filters
    from ssios.library_filter import parse as parse_filters

    smart_row = None
    if smart_id:
        from ssios.library_query import smart_filter, smart_row as find_smart

        try:
            smart_row = find_smart(plane, account_id, library_id, smart_id)
        except SsiosError:
            smart_row = {}
        items = smart_filter(catalog, smart_row) if smart_row else []
    else:
        items = filter_items(
            catalog,
            folder=folder,
            tag=tag,
            trash=trash,
            uncategorized=uncategorized,
            untagged=untagged,
        )
    suggested_view = str(query.get("suggested") or "") == "1"
    if suggested_view:
        items = [row for row in filter_items(catalog) if row.get("suggested_tags")]
    filters = parse_filters(query)
    items = apply_filters(items, filters)
    ranked = False
    if text_query:
        from ssios.library_query import _blob

        from ssios.library_search import by_meaning, meaning_ids

        from ssios.library_chat import tag_filter

        names = {str(row.get("id") or ""): str(row.get("name") or "") for row in meta.get("folders") or [] if isinstance(row, dict)}
        for item in items:
            item["_folder_names"] = " ".join(names.get(str(one), "") for one in item.get("folder_ids") or [])
        wanted, rest = tag_filter(text_query)
        if wanted:
            items = [item for item in items if all(one in {str(name).casefold() for name in item.get("tags") or []} for one in wanted)]
        needle = rest.casefold()
        if needle:
            close = [] if trash else meaning_ids(plane, account_id, library_id, rest)
            # Every word must appear somewhere, in any order. Search by meaning adds what the words miss.
            words = needle.split()
            items = by_meaning([item for item in items if all(word in _blob(item) for word in words) or str(item.get("id") or "") in close], close, needle)
            ranked = bool(close) and "sort" not in query
    selected = None
    item_id = str(query.get("item") or "")
    if item_id:
        try:
            selected = get_item(plane, account_id, library_id, item_id)
        except SsiosError:
            selected = None
        if selected is not None and not selected.get("trashed"):
            # Opening a file in the inspector is the account looking at it.
            from ssios.reading import mark_file

            try:
                mark_file(plane, session["account"], library_id, item_id, "skimmed", "opened")
            except SsiosError:
                pass
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
    from urllib.parse import quote as _quote

    from ssios.library import tag_counts

    tag_rows = tag_counts(plane, account_id, library_id, catalog)
    tag_list = '<datalist id="lib-tag-names">' + "".join(f'<option value="{html.escape(name, quote=True)}">' for name, _count in tag_rows) + "</datalist>"
    from ssios.library_tags import colour_of, groups_of

    tag_groups, tag_owner = groups_of(meta)

    def tag_link(name: str, count: int) -> str:
        mark = ' class="active"' if name == tag else ""
        return f'<a{mark} href="{base}&amp;tag={html.escape(_quote(name, safe=""), quote=True)}">{html.escape(name)} <span class="count">{count}</span></a>'

    counted = dict(tag_rows)
    for group in tag_groups:
        members = [name for name, _count in tag_rows if tag_owner.get(name.casefold()) is group]
        if not members:
            continue
        dot = colour_of(str(group.get("color") or ""))
        tags.append(
            f'<p class="lib-tag-group"><span class="dot" style="background:{html.escape(dot or "transparent", quote=True)}"></span>{html.escape(str(group.get("name") or ""))}</p>'
            + "".join(tag_link(name, counted[name]) for name in members)
        )
    loose = [(name, count) for name, count in tag_rows if name.casefold() not in tag_owner]
    if loose and tags:
        tags.append(f'<p class="lib-tag-group">{html.escape(say(lang, "lib_ungrouped"))}</p>')
    tags.extend(tag_link(name, count) for name, count in loose)
    if tag_rows:
        tags.append(f'<a class="lib-manage" href="{base}&amp;manage=tags">{html.escape(say(lang, "lib_manage_tags"))}</a>')
    from ssios.library_filter import sort_items, sort_of

    sort, descending = sort_of(query)
    if not ranked:
        items = sort_items(items, sort, descending)
    counts = {
        "all": len(filter_items(catalog)),
        "uncategorized": len(filter_items(catalog, uncategorized=True)),
        "untagged": len(filter_items(catalog, untagged=True)),
        "trash": len(filter_items(catalog, trash=True)),
        "suggested": sum(1 for row in filter_items(catalog) if row.get("suggested_tags")),
    }
    cards = []
    try:
        shown = max(PAGE_SIZE, min(int(str(query.get("limit") or PAGE_SIZE)), 100_000))
    except ValueError:
        shown = PAGE_SIZE
    total = len(items)
    more = ""
    if total > shown:
        from urllib.parse import urlencode

        keep = {key: str(value) for key, value in query.items() if key not in {"limit", "item", "csrf"} and isinstance(value, str)}
        keep["limit"] = str(shown + PAGE_SIZE)
        more = (
            f'<p class="lib-more"><a href="/ui/library?{html.escape(urlencode(keep), quote=True)}">'
            f'{html.escape(say(lang, "lib_more", shown=shown, total=total))}</a></p>'
        )
        items = items[:shown]
    for item in items:
        ident = html.escape(str(item.get("id") or ""), quote=True)
        label = html.escape(str(item.get("name") or ""))
        ext = html.escape(str(item.get("ext") or "").upper())
        tag_attr = html.escape("|".join(str(tag) for tag in item.get("tags") or []), quote=True)
        check = f'<input class="lib-check" type="checkbox" aria-label="{html.escape(say(lang, "lib_check"), quote=True)}">'
        avatar = _adder_avatar(plane, item) if chosen.get("kind") == "shared" else ""
        if item.get("group") == "image" and item.get("ext") in {"png", "jpg", "gif", "webp", "bmp"}:
            thumb = f'<img class="lib-thumb" src="/ui/library/raw?library={html.escape(library_id, quote=True)}&amp;item={ident}" alt="" data-tile="{ext}">'
        else:
            thumb = f'<img class="lib-thumb" src="/ui/library/thumb?library={html.escape(library_id, quote=True)}&amp;item={ident}" alt="" data-tile="{ext}">'
        cards.append(
            f'<a class="lib-card" data-item="{ident}" data-size="{int(item.get("size") or 0)}" data-tags="{tag_attr}" href="{base}&amp;item={ident}">{check}{avatar}{thumb}<span class="name">{label}</span><span class="type-badge">{ext}</span></a>'
        )
    inspector = ""
    if selected:
        ident = html.escape(str(selected.get("id") or ""), quote=True)
        ext = html.escape(str(selected.get("ext") or "").upper())
        width, height = _preview_box(selected)
        if selected.get("group") == "image" and selected.get("ext") in {"png", "jpg", "gif", "webp", "bmp"}:
            src = f"/ui/library/raw?library={html.escape(library_id, quote=True)}&amp;item={ident}"
        else:
            src = f"/ui/library/thumb?library={html.escape(library_id, quote=True)}&amp;item={ident}"
        preview = f'<img class="lib-preview" src="{src}" width="{width}" height="{height}" alt="" data-tile="{ext}">'
        versions = len(selected.get("versions") or [])
        version_label = say(
            lang,
            "lib_meta",
            kind=str(selected.get("ext") or "").upper(),
            size=_human_size(int(selected.get("size") or 0)),
            versions=say(lang, "lib_versions", count=versions),
        )
        star = int(selected.get("star") or 0)
        stars = "".join(
            f'<button type="submit" name="star" value="{n}" class="{"on" if n <= star else ""}" aria-label="{n}">{"★" if n <= star else "☆"}</button>'
            for n in range(1, 6)
        )
        from urllib.parse import quote as _quote_tag

        chips = "".join(
            f'<form method="post" action="/ui/library" class="lib-chip">{_hidden(csrf, library_id, ident)}'
            f'<input type="hidden" name="op" value="untag-item"><input type="hidden" name="tag" value="{html.escape(str(one), quote=True)}">'
            f'<a href="{base}&amp;tag={html.escape(_quote_tag(str(one), safe=""), quote=True)}">{html.escape(str(one))}</a>'
            f'<button type="submit" aria-label="{html.escape(say(lang, "lib_untag_one", tag=str(one)), quote=True)}">×</button></form>'
            for one in selected.get("tags") or []
        )
        inspector = (
            preview
            + f'<h2 class="lib-rename" data-name="{html.escape(str(selected.get("name") or ""), quote=True)}" data-id="{ident}">'
            + f'<span class="lib-rename-text">{html.escape(str(selected.get("name") or ""))}</span>'
            + f'<button type="button" class="lib-rename-edit" aria-label="{html.escape(say(lang, "lib_rename"), quote=True)}">{html.escape(say(lang, "lib_rename"))}</button></h2>'
            + '<p class="lib-rename-error" hidden></p>'
            + f"<p>{html.escape(version_label)}</p>"
            + (f'<p class="lib-dims">{html.escape(_dims(selected, lang))}</p>' if _dims(selected, lang) else "")
            + _swatches(selected, base)
            + (f'<p class="lib-source"><a href="{html.escape(str(selected.get("source_url")), quote=True)}" rel="noopener noreferrer">{html.escape(str(selected.get("source_url"))[:80])}</a></p>' if str(selected.get("source_url") or "").startswith(("https://", "http://")) else "")
            + (f"<p>{html.escape(str(selected.get('captured_at') or ''))}</p>" if selected.get("captured_at") else "")
            + f"<form method=\"post\" action=\"/ui/library\">{_hidden(csrf, library_id, ident)}"
            + f'<input type="hidden" name="op" value="star">'
            + f'<p>{html.escape(say(lang, "lib_star"))}</p><div class="lib-stars">{stars}</div></form>'
            + f'<div class="lib-chips">{chips}</div>'
            + _suggestions(selected, lang, csrf, library_id, ident)
            + f'<form method="post" action="/ui/library">{_hidden(csrf, library_id, ident)}'
            + '<input type="hidden" name="op" value="tag-item">'
            + f'<label>{html.escape(say(lang, "lib_tag"))} <input name="tag" maxlength="200" list="lib-tag-names" placeholder="a, b" autocomplete="off"></label>'
            + f'<button type="submit">{html.escape(say(lang, "lib_add_tag"))}</button></form>'
            + _analysis_block(selected, lang, csrf, library_id, ident, _depth(plane, account_id, library_id, selected))
        )
        if selected.get("trashed"):
            inspector += (
                f'<div class="lib-actions"><form method="post" action="/ui/library">{_hidden(csrf, library_id, ident)}'
                + '<input type="hidden" name="op" value="restore">'
                + f'<button type="submit">{html.escape(say(lang, "lib_restore"))}</button></form>'
            )
            if chosen.get("role") == "owner":
                inspector += (
                    f'<form method="post" action="/ui/library">{_hidden(csrf, library_id, ident)}'
                    + '<input type="hidden" name="op" value="purge">'
                    + f'<label>{html.escape(say(lang, "lib_purge_hint"))} <input name="confirm"></label>'
                    + f'<button type="submit">{html.escape(say(lang, "lib_purge"))}</button></form>'
                )
            inspector += "</div>"
        else:
            inspector += (
                f'<form method="post" action="/ui/library">{_hidden(csrf, library_id, ident)}'
                + '<input type="hidden" name="op" value="assign">'
                + f'<label>{html.escape(say(lang, "lib_folder"))} <select name="folder">'
                + "".join(
                    f'<option value="{html.escape(str(row.get("id") or ""), quote=True)}">{html.escape(str(row.get("name") or ""))}</option>'
                    for row in meta.get("folders") or []
                    if isinstance(row, dict)
                )
                + "</select></label>"
                + f'<button type="submit">{html.escape(say(lang, "lib_add_folder"))}</button></form>'
                + f'<div class="lib-actions"><form method="post" action="/ui/library">{_hidden(csrf, library_id, ident)}'
                + '<input type="hidden" name="op" value="trash">'
                + f'<button type="submit">{html.escape(say(lang, "lib_trash_item"))}</button></form></div>'
            )
    nav = _nav(base, lang, counts, folder, trash, uncategorized, untagged, smart_id, suggested_view)
    left = (
        '<div class="lib-side">'
        + "".join(switcher)
        + nav
        + (
            '<details class="lib-section" open><summary>'
            + html.escape(say(lang, "lib_tags"))
            + '</summary><div class="lib-tree lib-tags">'
            + "".join(tags)
            + "</div></details>"
            if tags
            else ""
        )
        + '<details class="lib-section" open><summary>'
        + html.escape(say(lang, "lib_folders"))
        + "</summary>"
        + '<div class="lib-tree">'
        + ("".join(folders) or "")
        + "</div>"
        + "<details><summary>"
        + html.escape(say(lang, "lib_add"))
        + "</summary>"
        + f'<form method="post" action="/ui/library"><input type="hidden" name="csrf" value="{csrf}">'
        + f'<input type="hidden" name="library" value="{html.escape(library_id, quote=True)}">'
        + '<input type="hidden" name="op" value="folder">'
        + f'<label>{html.escape(say(lang, "lib_folder"))} <input name="name" maxlength="40"></label>'
        + f'<button type="submit">{html.escape(say(lang, "lib_add_folder_new"))}</button></form></details></details>'
        + '<details class="lib-section"><summary>'
        + html.escape(say(lang, "smart_folder"))
        + "</summary>"
        + _smart_block(meta, base, lang, csrf, library_id)
        + "</details>"
        + '<details class="lib-settings"><summary>'
        + html.escape(say(lang, "lib_settings"))
        + "</summary>"
        + f'<form method="post" action="/ui/library"><input type="hidden" name="csrf" value="{csrf}">'
        + '<input type="hidden" name="op" value="create-library">'
        + f'<label>{html.escape(say(lang, "lib_new_library"))} <input name="name" maxlength="40"></label>'
        + (f'<label>{html.escape(say(lang, "lib_location"))} <input name="location" maxlength="400" placeholder="/Volumes/Disk/Libraries"></label>' if admin else "")
        + f'<button type="submit">{html.escape(say(lang, "lib_create"))}</button></form>'
        + f'<p>{html.escape(say(lang, "lib_where"))}: <code>{html.escape(str(chosen.get("location") or say(lang, "lib_where_default")))}</code></p>'
        + (
            f'<form method="post" action="/ui/library"><input type="hidden" name="csrf" value="{csrf}">'
            f'<input type="hidden" name="library" value="{html.escape(library_id, quote=True)}"><input type="hidden" name="op" value="move-library">'
            f'<label>{html.escape(say(lang, "lib_move"))} <input name="location" maxlength="400" placeholder="/Volumes/Disk/Libraries"></label>'
            f'<button type="submit">{html.escape(say(lang, "lib_move"))}</button></form>'
            if chosen.get("role") == "owner" and admin
            else ""
        )
        + _watch_form(meta, lang, csrf, library_id)
        + (_auto_analyze_form(meta, lang, csrf, library_id) if chosen.get("kind") != "shared" or chosen.get("role") == "owner" else "")
        + (_rule_tags_form(meta, lang, csrf, library_id) if chosen.get("kind") != "shared" or chosen.get("role") == "owner" else "")
        + _share_block(chosen, lang, csrf, library_id)
        + "</details></div>"
    )
    empty = counts["all"] == 0 and not trash
    zone = ""
    if empty:
        zone = f'<div class="drop-zone"><p>{html.escape(say(lang, "lib_drop_empty"))}</p></div>'
    grid = (
        '<div class="lib-toolbar">'
        + _filter_bar(query, filters, lang, library_id, text_query, sort, descending, csrf, chosen, smart_row)
        + tag_list
        + (_folder_tags_form(meta, folder, lang, csrf, library_id) if folder and str(chosen.get("role") or "owner") in {"owner", "organize"} else "")
        + (_suggested_bar(lang, csrf, library_id, counts.get("suggested", 0)) if suggested_view else "")
        + f'<form id="library-import" class="lib-import" method="post" action="/ui/library" enctype="multipart/form-data">'
        + f'<input type="hidden" name="csrf" value="{csrf}">'
        + f'<input type="hidden" name="library" value="{html.escape(library_id, quote=True)}">'
        + '<input type="hidden" name="op" value="import">'
        + '<input type="hidden" name="source" value="picker">'
        + f'<label class="button">{html.escape(say(lang, "lib_import"))} <input name="file" type="file" multiple></label>'
        + f'<label class="button">{html.escape(say(lang, "import_folder"))} <input name="folder_files" type="file" webkitdirectory multiple></label>'
        + f'<label class="lib-import-tags">{html.escape(say(lang, "import_tags"))} <input id="lib-import-tags" name="tags" maxlength="200" placeholder="a, b" list="lib-tag-names"></label>'
        + "</form>"
        + f'<details class="lib-link"><summary>{html.escape(say(lang, "import_link"))}</summary>'
        + f'<form method="post" action="/ui/library"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="library" value="{html.escape(library_id, quote=True)}">'
        + f'<input type="hidden" name="op" value="import-url"><input type="hidden" name="folder" value="{html.escape(folder, quote=True)}">'
        + f'<p class="muted small">{html.escape(say(lang, "import_link_hint"))}</p>'
        + '<input name="url" type="url" required placeholder="https://example.com/picture.png" maxlength="2000">'
        + f'<input name="tags" placeholder="{html.escape(say(lang, "import_tags"), quote=True)}" maxlength="200" list="lib-tag-names">'
        + f'<button type="submit">{html.escape(say(lang, "import_link"))}</button></form></details>'
        + "</div>"
        + zone
        + '<div id="library-grid" class="lib-grid">'
        + ("".join(cards) or f"<p>{html.escape(say(lang, 'lib_empty_view'))}</p>")
        + "</div>"
        + more
        + _batch_bar(lang, str(chosen.get("role") or "owner"), trash, bool(folder))
    )
    if str(query.get("manage") or "") == "tags":
        grid = tag_list + _tag_manager(lang, csrf, library_id, base, meta, tag_rows, str(chosen.get("role") or "owner"), str(query.get("tq") or ""))
        selected = None
    selected_class = " has-selection" if selected else ""
    body = (
        notice
        + f'<div class="library{selected_class}" id="library-app" data-library="{html.escape(library_id, quote=True)}" data-csrf="{csrf}" '
        + f'data-overlay="{html.escape(say(lang, "drop_overlay"), quote=True)}" '
        + f'data-directory="{html.escape(say(lang, "drop_directory"), quote=True)}" '
        + f'data-too-deep="{html.escape(say(lang, "drop_too_deep", depth=12), quote=True)}" data-too-many="{html.escape(say(lang, "drop_too_many", count=5000), quote=True)}" '
        + f'data-keep="{html.escape(say(lang, "conflict_keep"), quote=True)}" '
        + f'data-replace="{html.escape(say(lang, "conflict_replace"), quote=True)}" '
        + f'data-apply="{html.escape(say(lang, "conflict_apply"), quote=True)}" '
        + f'data-rename="{html.escape(say(lang, "lib_rename"), quote=True)}" '
        + f'data-role="{html.escape(str(chosen.get("role") or "owner"), quote=True)}" '
        + f'data-trash="{"1" if trash else "0"}" '
        + f'data-folder="{html.escape(folder, quote=True)}" '
        + f'data-selected="{html.escape(say(lang, "lib_selected", count="{count}"), quote=True)}" '
        + f'data-summary="{html.escape(say(lang, "drop_summary", saved="{saved}", versions="{versions}", skipped="{skipped}"), quote=True)}">'
        + left
        + '<div class="lib-main">'
        + '<ol id="drop-log" class="drop-log"></ol><p id="drop-summary"></p>'
        + grid
        + "</div>"
        + (f'<aside class="lib-inspector" id="lib-single">{inspector}</aside>' if selected else "")
        + _multi_panel(lang)
        + _batch_dialogs(lang, known)
        + f'<div id="drop-overlay" class="drop-overlay">{html.escape(say(lang, "drop_overlay"))}</div></div>'
    )
    body += f'<script src="{library_script_path()}"></script>'
    return _page(session, say(lang, "library_link"), body, bleed=True)


def library_post(plane, session: dict, body: dict, peer_ip: str) -> Page:
    try:
        return _library_post(plane, session, body, peer_ip)
    except SsiosError as exc:
        if not str(exc.code).startswith("library_"):
            raise
        if str(body.get("ajax") or "") == "1":
            import json

            payload = json.dumps({"ok": False, "error": exc.detail}, ensure_ascii=False)
            return Page(200, payload, content_type="application/json; charset=utf-8")
        library_id = str(body.get("library") or "")
        notice = f'<p class="notice">{html.escape(exc.detail)}</p>'
        return Page(200, library_page(plane, session, {"library": library_id, "item": str(body.get("item") or "")}, notice))


def _library_post(plane, session: dict, body: dict, peer_ip: str) -> Page:
    account_id = session["account"]["id"]
    op = str(body.get("op") or "")
    library_id = str(body.get("library") or "")
    # A folder path makes this computer write somewhere outside the state directory. On the page only an admin
    # may name one. Any account can have one set up by the operator with ssios library-create or library-move.
    may_place = str(session["account"].get("role") or "") == "admin"
    if op == "create-library":
        where = str(body.get("location") or "").strip()
        if where and not may_place:
            raise SsiosError("library_denied", say(_language(plane, account_id), "library_denied"), 403)
        created = create_library(plane, account_id, str(body.get("name") or ""), peer_ip, where)
        return Page(200, library_page(plane, session, {"library": created["id"]}, "<p>Created.</p>"))
    chosen = library_or_default(plane, account_id, library_id)
    library_id = chosen["id"]
    if op == "move-library":
        if not may_place:
            raise SsiosError("library_denied", say(_language(plane, account_id), "library_denied"), 403)
        from ssios.library_storage import move_library

        moved = move_library(plane, account_id, library_id, str(body.get("location") or ""), peer_ip)
        return Page(200, library_page(plane, session, {"library": library_id}, f"<p>Moved {moved['files']} files. The old copy was set aside, not deleted.</p>"))
    if op == "resolve":
        from ssios.library import resolve_conflict

        decided = resolve_conflict(plane, account_id, library_id, str(body.get("conflict") or ""), str(body.get("decision") or ""), peer_ip)
        return Page(200, library_page(plane, session, {"library": library_id, "item": decided.get("id") or ""}, f"<p>{html.escape(decided.get('reason') or '')}</p>"))
    if op == "batch":
        import json

        from ssios.library_batch import batch_apply

        result = batch_apply(
            plane,
            account_id,
            library_id,
            str(body.get("ids") or ""),
            str(body.get("action") or ""),
            peer_ip,
            tag=str(body.get("tag") or ""),
            folder_id=str(body.get("folder") or ""),
            confirm=str(body.get("confirm") or ""),
        )
        if str(body.get("ajax") or "") == "1":
            return Page(200, json.dumps(result, ensure_ascii=False), content_type="application/json; charset=utf-8")
        return Page(200, library_page(plane, session, {"library": library_id}, ""))
    if op == "download":
        from ssios.library_batch import zip_items

        payload = zip_items(plane, account_id, library_id, str(body.get("ids") or ""), peer_ip)
        return Page(200, payload, content_type="application/zip", extra_headers={"Content-Disposition": 'attachment; filename="files.zip"'})
    if op == "rename":
        import json

        from ssios.library import rename_item

        renamed = rename_item(plane, account_id, library_id, str(body.get("item") or ""), str(body.get("name") or ""), peer_ip)
        if str(body.get("ajax") or "") == "1":
            payload = json.dumps({"ok": True, "id": renamed["id"], "name": renamed["name"]}, ensure_ascii=False)
            return Page(200, payload, content_type="application/json; charset=utf-8")
        return Page(200, library_page(plane, session, {"library": library_id, "item": renamed["id"]}, ""))
    if op == "import":
        raw, filename, source = _upload(body)
        saved = import_bytes(plane, account_id, library_id, raw, name=filename, source=source, peer_ip=peer_ip)
        if saved.get("result") == "conflict":
            return Page(200, library_page(plane, session, {"library": library_id}, _conflict_form(session, library_id, saved, _language(plane, account_id))))
        return Page(200, library_page(plane, session, {"library": library_id, "item": saved["id"]}, "<p>Saved the file.</p>"))
    if op == "import-url":
        from ssios.library import clean_tags
        from ssios.library_import import import_url

        saved = import_url(plane, account_id, library_id, str(body.get("url") or ""), peer_ip, folder_id=str(body.get("folder") or ""), tags=clean_tags(body.get("tags") or ""))
        if saved.get("result") == "conflict":
            return Page(200, library_page(plane, session, {"library": library_id}, _conflict_form(session, library_id, saved, _language(plane, account_id))))
        return Page(200, library_page(plane, session, {"library": library_id, "item": saved["id"]}, "<p>Saved the file.</p>"))
    if op == "smart-filters":
        from ssios.library_query import add_filter_folder

        filters = {key[2:]: str(value) for key, value in body.items() if key.startswith("f_") and isinstance(value, str)}
        row = add_filter_folder(plane, account_id, library_id, str(body.get("name") or ""), filters, str(body.get("q") or ""), peer_ip)
        return Page(200, library_page(plane, session, {"library": library_id, "smart": row["id"]}, f"<p>{html.escape(say(_language(plane, account_id), 'smart_saved'))}</p>"))
    if op == "smart":
        from ssios.library_query import add_smart_folder

        add_smart_folder(plane, account_id, library_id, str(body.get("name") or ""), str(body.get("query") or ""), peer_ip)
        return Page(200, library_page(plane, session, {"library": library_id}, f"<p>{html.escape(say(_language(plane, account_id), 'smart_saved'))}</p>"))
    if op == "auto_analyze":
        from ssios.background import set_auto_analyze

        set_auto_analyze(plane, account_id, library_id, bool(body.get("enabled")), peer_ip)
        return Page(200, library_page(plane, session, {"library": library_id}, f"<p>{html.escape(say(_language(plane, account_id), 'auto_analyze_saved'))}</p>"))
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
    if op in {"opened", "mark-read", "remind-read"}:
        from ssios.reading import mark_file, remind_to_read
        from ssios.scope import AccountScope

        item = get_item(plane, account_id, library_id, str(body.get("item") or ""))
        if op == "remind-read":
            when = remind_to_read(plane, AccountScope(plane, session["account"]), f"file:{library_id}:{item['id']}", str(item.get("name") or ""), str(body.get("when") or "tomorrow"))
            return Page(200, library_page(plane, session, {"library": library_id, "item": item["id"]}, f"<p>Reminder set for {html.escape(when)}.</p>"))
        mark_file(plane, session["account"], library_id, item["id"], "read" if op == "mark-read" else "skimmed", "marked" if op == "mark-read" else "opened", peer_ip)
        if str(body.get("ajax") or "") == "1":
            return Page(200, '{"ok": true}', content_type="application/json; charset=utf-8")
        return Page(200, library_page(plane, session, {"library": library_id, "item": item["id"]}))
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
        from ssios.library import add_item_tags, clean_tags

        wanted = clean_tags(body.get("tag"))
        if not wanted:
            raise SsiosError("library_rejected", say(_language(plane, account_id), "library_rejected"), 400)
        add_item_tags(plane, account_id, library_id, str(body.get("item") or ""), wanted, peer_ip)
    elif op == "untag-item":
        from ssios.library_tags import remove_item_tag

        remove_item_tag(plane, account_id, library_id, str(body.get("item") or ""), str(body.get("tag") or ""), peer_ip)
    elif op in {"suggest-accept", "suggest-dismiss"}:
        from ssios.library_autotag import review

        value = str(body.get("tag") or "")
        value_or_all = "*" if value == "*" else value
        review(plane, account_id, library_id, str(body.get("item") or ""), peer_ip, accept=value_or_all if op == "suggest-accept" else None, dismiss=value_or_all if op == "suggest-dismiss" else None)
    elif op == "suggest-all":
        from ssios.library_autotag import review_all

        review_all(plane, account_id, library_id, peer_ip, accept=str(body.get("decision") or "") == "accept")
        return Page(200, library_page(plane, session, {"library": library_id}))
    elif op == "folder-tags":
        from ssios.library_autotag import set_folder_tags

        folder_id = str(body.get("folder") or "")
        result = set_folder_tags(plane, account_id, library_id, folder_id, str(body.get("tags") or ""), peer_ip, apply_now=bool(body.get("apply")))
        zh = _language(plane, account_id) == "zh"
        note = (f"已保存。加到了 {result['items']} 个已有文件。" if zh else f"Saved. Added to {_files(result['items'])} already in the folder.") if body.get("apply") else ("已保存。" if zh else "Saved.")
        return Page(200, library_page(plane, session, {"library": library_id, "folder": folder_id}, f'<p class="notice">{html.escape(note)}</p>'))
    elif op == "rule-tags":
        from ssios.library_autotag import RULES, set_rules

        set_rules(plane, account_id, library_id, {name: bool(body.get(name)) for name in RULES}, peer_ip)
    elif op in {"tag-rename", "tag-delete", "tag-group", "group-add", "group-update", "group-delete"}:
        return _tag_manager_post(plane, session, body, peer_ip, op, library_id)
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


def library_thumb(plane, session: dict, query: dict) -> Page:
    """Always an image. A PDF is a rendered page or the type tile, never the PDF bytes."""
    from ssios.library_thumb import png_size, preview_png

    account_id = session["account"]["id"]
    chosen = library_or_default(plane, account_id, str(query.get("library") or ""))
    item = get_item(plane, account_id, chosen["id"], str(query.get("item") or ""))
    data = preview_png(plane, account_id, chosen["id"], item)
    return Page(200, data, content_type="image/png" if data.startswith(b"\x89PNG") else _image_type(item))


def _image_type(item: dict) -> str:
    ext = str(item.get("ext") or "")
    return {"jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp"}.get(ext, "image/png")


def _preview_box(item: dict) -> tuple[int, int]:
    if item.get("ext") == "pdf" or item.get("group") != "image":
        return (160, 200)
    return (160, 160)


def _suggestions(item: dict, lang: str, csrf: str, library_id: str, ident: str) -> str:
    """Tags the model suggested. Each waits for the owner: accept it, or dismiss it so it is not offered again."""
    offered = [str(tag) for tag in item.get("suggested_tags") or []]
    if not offered:
        return ""
    zh = lang == "zh"
    hidden = _hidden(csrf, library_id, ident)
    chips = "".join(
        f'<span class="lib-suggest"><span>{html.escape(tag)}</span>'
        f'<form method="post" action="/ui/library">{hidden}<input type="hidden" name="op" value="suggest-accept"><input type="hidden" name="tag" value="{html.escape(tag, quote=True)}">'
        f'<button type="submit" aria-label="{html.escape(("接受 " if zh else "Accept ") + tag, quote=True)}">✓</button></form>'
        f'<form method="post" action="/ui/library">{hidden}<input type="hidden" name="op" value="suggest-dismiss"><input type="hidden" name="tag" value="{html.escape(tag, quote=True)}">'
        f'<button type="submit" aria-label="{html.escape(("忽略 " if zh else "Dismiss ") + tag, quote=True)}">×</button></form></span>'
        for tag in offered
    )
    return (
        f'<div class="lib-suggestions"><p class="muted small">{html.escape("模型建议的标签，接受后才会加上：" if zh else "Suggested by the model. Not added until you accept:")}</p>'
        f'<div class="lib-chips">{chips}</div><div class="lib-actions">'
        f'<form method="post" action="/ui/library">{hidden}<input type="hidden" name="op" value="suggest-accept"><input type="hidden" name="tag" value="*">'
        f'<button type="submit">{html.escape("全部接受" if zh else "Accept all")}</button></form>'
        f'<form method="post" action="/ui/library">{hidden}<input type="hidden" name="op" value="suggest-dismiss"><input type="hidden" name="tag" value="*">'
        f'<button type="submit">{html.escape("全部忽略" if zh else "Dismiss all")}</button></form></div></div>'
    )


def _suggested_bar(lang: str, csrf: str, library_id: str, count: int) -> str:
    zh = lang == "zh"
    hidden = _hidden(csrf, library_id)
    return (
        f'<div class="lib-suggest-bar"><p>{html.escape(f"{count} 个文件有待确认的标签。" if zh else f"{count} files have suggested tags.")}</p>'
        f'<form method="post" action="/ui/library">{hidden}<input type="hidden" name="op" value="suggest-all"><input type="hidden" name="decision" value="accept">'
        f'<button type="submit" data-confirm="{html.escape("接受所有文件上的全部建议标签？" if zh else "Accept every suggested tag on every file?", quote=True)}">{html.escape("全部接受" if zh else "Accept all")}</button></form>'
        f'<form method="post" action="/ui/library">{hidden}<input type="hidden" name="op" value="suggest-all"><input type="hidden" name="decision" value="dismiss">'
        f'<button type="submit" data-confirm="{html.escape("忽略所有建议标签？" if zh else "Dismiss every suggested tag?", quote=True)}">{html.escape("全部忽略" if zh else "Dismiss all")}</button></form></div>'
    )


def _folder_tags_form(meta: dict, folder: str, lang: str, csrf: str, library_id: str) -> str:
    row = next((one for one in meta.get("folders") or [] if isinstance(one, dict) and one.get("id") == folder), None)
    if row is None:
        return ""
    zh = lang == "zh"
    current = ", ".join(str(tag) for tag in row.get("auto_tags") or [])
    title = ("文件夹自动标签" if zh else "Folder auto-tags") + (f": {current}" if current else "")
    return (
        f'<details class="lib-folder-tags"><summary>{html.escape(title)}</summary>'
        f'<form method="post" action="/ui/library">{_hidden(csrf, library_id)}<input type="hidden" name="op" value="folder-tags"><input type="hidden" name="folder" value="{html.escape(folder, quote=True)}">'
        f'<p class="muted small">{html.escape("导入或移入这个文件夹的文件会自动加上这些标签。" if zh else "Files imported or moved into this folder get these tags.")}</p>'
        f'<input name="tags" value="{html.escape(current, quote=True)}" maxlength="400" list="lib-tag-names" placeholder="a, b" autocomplete="off">'
        f'<label><input type="checkbox" name="apply" value="1"> {html.escape("也加到已在文件夹里的文件" if zh else "Also add them to the files already here")}</label>'
        f'<button type="submit">{html.escape("保存" if zh else "Save")}</button></form></details>'
    )


def _rule_tags_form(meta: dict, lang: str, csrf: str, library_id: str) -> str:
    from ssios.library_autotag import rules_of

    zh = lang == "zh"
    rules = rules_of(meta)
    labels = {
        "type": ("Add the file type as a tag (image, video, document…)", "把文件类型加为标签（图片、视频、文档…）"),
        "orientation": ("Add the orientation as a tag (landscape, portrait, square)", "把方向加为标签（横向、纵向、方形）"),
        "folder_names": ("Add the names of a dropped folder's subfolders as tags", "把拖入文件夹里的子文件夹名加为标签"),
    }
    boxes = "".join(
        f'<label><input type="checkbox" name="{name}" value="1"{" checked" if rules[name] else ""}> {html.escape(labels[name][1 if zh else 0])}</label>'
        for name in labels
    )
    return (
        f'<form method="post" action="/ui/library" class="lib-watch">{_hidden(csrf, library_id)}<input type="hidden" name="op" value="rule-tags">'
        f'<p>{html.escape("导入新文件时自动加的标签，默认关闭。" if zh else "Tags added to new files at import. All off until you turn them on.")}</p>'
        f'{boxes}<button type="submit">{html.escape("保存自动标签" if zh else "Save automatic tags")}</button></form>'
    )


def _tag_manager(lang: str, csrf: str, library_id: str, base: str, meta: dict, tag_rows: list, role: str, needle: str) -> str:
    """Every tag with its count. Rename (onto an existing name merges), group, and delete, as in Eagle's tag manager."""
    from urllib.parse import quote as _quote

    from ssios.library_tags import NAMED, colour_of, groups_of

    zh = lang == "zh"

    def t(en: str, cn: str) -> str:
        return html.escape(cn if zh else en)

    def esc(value) -> str:
        return html.escape(str(value), quote=True)

    can = role in {"owner", "organize"}
    groups, owner = groups_of(meta)
    hidden = f'<input type="hidden" name="csrf" value="{esc(csrf)}"><input type="hidden" name="library" value="{esc(library_id)}">'
    rows = []
    shown = [(name, count) for name, count in tag_rows if needle.casefold() in name.casefold()]
    for name, count in shown:
        group = owner.get(name.casefold())
        dot = colour_of(str(group.get("color") or "")) if group else ""
        link = f'<a href="{base}&amp;tag={esc(_quote(name, safe=""))}">{html.escape(name)}</a>'
        actions = ""
        if can:
            choices = f'<option value="">{t("No group", "不分组")}</option>' + "".join(
                f'<option value="{esc(row.get("id") or "")}"{" selected" if row is group else ""}>{html.escape(str(row.get("name") or ""))}</option>' for row in groups
            )
            actions = (
                f'<form method="post" action="/ui/library" class="lib-tm-rename">{hidden}<input type="hidden" name="op" value="tag-rename"><input type="hidden" name="old" value="{esc(name)}">'
                f'<input name="new" value="{esc(name)}" maxlength="40" list="lib-tag-names" aria-label="{t("New name", "新名称")}" autocomplete="off">'
                f'<button type="submit">{t("Rename or merge", "改名或合并")}</button></form>'
                + (
                    f'<form method="post" action="/ui/library">{hidden}<input type="hidden" name="op" value="tag-group"><input type="hidden" name="tag" value="{esc(name)}">'
                    f'<select name="group" aria-label="{t("Group", "分组")}">{choices}</select><button type="submit">{t("Set group", "设分组")}</button></form>'
                    if groups
                    else ""
                )
                + f'<form method="post" action="/ui/library" class="lib-tm-delete">{hidden}<input type="hidden" name="op" value="tag-delete"><input type="hidden" name="tag" value="{esc(name)}">'
                f'<button type="submit" class="lib-danger" data-confirm="{esc(say(lang, "lib_tag_delete_confirm", tag=name, count=count))}">{t("Delete", "删除")}</button></form>'
            )
        rows.append(
            f'<tr><td><span class="dot" style="background:{esc(dot or "transparent")}"></span>{link}</td>'
            f'<td class="num">{count}</td><td>{html.escape(str(group.get("name") or "")) if group else ""}</td><td class="lib-tm-actions">{actions}</td></tr>'
        )
    palette = "".join(f'<option value="{key}">{key}</option>' for key in NAMED)
    group_rows = []
    for row in groups:
        dot = colour_of(str(row.get("color") or ""))
        members = len(row.get("tags") or [])
        manage = ""
        if can:
            current = str(row.get("color") or "")
            colours = f'<option value="">{t("No colour", "无颜色")}</option>' + "".join(f'<option value="{key}"{" selected" if key == current else ""}>{key}</option>' for key in NAMED)
            if current and current not in NAMED:
                colours += f'<option value="{esc(current)}" selected>{html.escape(current)}</option>'
            manage = (
                f'<form method="post" action="/ui/library">{hidden}<input type="hidden" name="op" value="group-update"><input type="hidden" name="group" value="{esc(row.get("id") or "")}">'
                f'<input name="name" value="{esc(row.get("name") or "")}" maxlength="40" aria-label="{t("Group name", "分组名称")}"><select name="color" aria-label="{t("Colour", "颜色")}">{colours}</select>'
                f'<button type="submit">{t("Save", "保存")}</button></form>'
                f'<form method="post" action="/ui/library">{hidden}<input type="hidden" name="op" value="group-delete"><input type="hidden" name="group" value="{esc(row.get("id") or "")}">'
                f'<button type="submit" class="lib-danger" data-confirm="{esc(say(lang, "lib_group_delete_confirm", group=str(row.get("name") or "")))}">{t("Delete group", "删除分组")}</button></form>'
            )
        group_rows.append(
            f'<li><span class="dot" style="background:{esc(dot or "transparent")}"></span><strong>{html.escape(str(row.get("name") or ""))}</strong> '
            f'<span class="muted">{say(lang, "lib_group_count", count=members)}</span><div class="lib-tm-actions">{manage}</div></li>'
        )
    add_group = ""
    if can:
        add_group = (
            f'<form method="post" action="/ui/library" class="lib-tm-add">{hidden}<input type="hidden" name="op" value="group-add">'
            f'<label>{t("New group", "新分组")} <input name="name" maxlength="40" required></label>'
            f'<label>{t("Colour", "颜色")} <select name="color"><option value="">{t("No colour", "无颜色")}</option>{palette}</select></label>'
            f'<button type="submit">{t("Add group", "添加分组")}</button></form>'
        )
    return (
        '<section class="lib-tag-manager">'
        + f'<p><a href="{base}">← {t("Back to files", "返回文件")}</a></p>'
        + f'<h2>{t("Tags", "标签")} <span class="muted">{len(tag_rows)}</span></h2>'
        + f'<form method="get" action="/ui/library" class="lib-tm-find"><input type="hidden" name="library" value="{esc(library_id)}"><input type="hidden" name="manage" value="tags">'
        + f'<input type="search" name="tq" value="{esc(needle)}" placeholder="{t("Find a tag", "查找标签")}"><button type="submit">{t("Find", "查找")}</button></form>'
        + f'<p class="muted small">{t("Renaming a tag onto a name that exists merges the two. Deleting a tag takes it off every file; the files stay.", "把标签改成已有的名称会把两个标签合并。删除标签会从所有文件上去掉它，文件本身保留。")}</p>'
        + '<div class="lib-tm-scroll"><table class="lib-tm"><thead><tr>'
        + f'<th>{t("Tag", "标签")}</th><th class="num">{t("Files", "文件")}</th><th>{t("Group", "分组")}</th><th></th></tr></thead><tbody>'
        + ("".join(rows) or f'<tr><td colspan="4">{t("No tags yet.", "还没有标签。")}</td></tr>')
        + "</tbody></table></div>"
        + f'<h2>{t("Tag groups", "标签分组")}</h2><ul class="lib-tm-groups">{"".join(group_rows)}</ul>'
        + add_group
        + "</section>"
    )


def _files(count: int) -> str:
    return "1 file" if count == 1 else f"{count} files"


def _tag_manager_post(plane, session: dict, body: dict, peer_ip: str, op: str, library_id: str) -> Page:
    from ssios.library_tags import add_group, delete_tag, rename_tag, set_tag_group, update_group

    account_id = session["account"]["id"]
    lang = _language(plane, account_id)
    zh = lang == "zh"
    if op == "tag-rename":
        result = rename_tag(plane, account_id, library_id, str(body.get("old") or ""), str(body.get("new") or ""), peer_ip)
        if result["merged"]:
            note = f'合并到“{result["name"]}”，改了 {result["items"]} 个文件。' if zh else f'Merged into "{result["name"]}" on {_files(result["items"])}.'
        else:
            note = f'已改名为“{result["name"]}”，{result["items"]} 个文件。' if zh else f'Renamed to "{result["name"]}" on {_files(result["items"])}.'
    elif op == "tag-delete":
        result = delete_tag(plane, account_id, library_id, str(body.get("tag") or ""), peer_ip)
        note = f'已从 {result["items"]} 个文件上去掉这个标签。' if zh else f'Removed the tag from {_files(result["items"])}.'
    elif op == "tag-group":
        set_tag_group(plane, account_id, library_id, str(body.get("tag") or ""), str(body.get("group") or ""), peer_ip)
        note = "已保存分组。" if zh else "Group saved."
    elif op == "group-add":
        add_group(plane, account_id, library_id, str(body.get("name") or ""), str(body.get("color") or ""), peer_ip)
        note = "已添加分组。" if zh else "Group added."
    elif op == "group-update":
        update_group(plane, account_id, library_id, str(body.get("group") or ""), peer_ip, name=str(body.get("name") or ""), colour=str(body.get("color") or ""))
        note = "已保存分组。" if zh else "Group saved."
    else:
        update_group(plane, account_id, library_id, str(body.get("group") or ""), peer_ip, delete=True)
        note = "已删除分组，标签保留。" if zh else "Group deleted. Its tags stay."
    return Page(200, library_page(plane, session, {"library": library_id, "manage": "tags"}, f'<p class="notice">{html.escape(note)}</p>'))


def _filter_bar(query: dict, filters: dict, lang: str, library_id: str, text_query: str, sort: str, descending: bool, csrf: str, chosen: dict, smart_row) -> str:
    """Keywords, sort, and Eagle's filters in one GET form, so a filtered view is a link."""
    from urllib.parse import urlencode

    from ssios.library_filter import ADDED, COLOURS, GROUPS, SHAPES, as_query, describe

    zh = lang == "zh"

    def esc(value) -> str:
        return html.escape(str(value), quote=True)

    def t(en: str, cn: str) -> str:
        return html.escape(cn if zh else en)

    def option(value: str, label: str, current: str) -> str:
        return f'<option value="{esc(value)}"{" selected" if value == current else ""}>{html.escape(label)}</option>'

    view = {key: str(query.get(key) or "") for key in ("folder", "tag", "smart", "trash", "uncategorized", "untagged") if str(query.get(key) or "")}
    hidden = f'<input type="hidden" name="library" value="{esc(library_id)}">' + "".join(f'<input type="hidden" name="{esc(key)}" value="{esc(value)}">' for key, value in view.items())
    sorts = (("added", "Date added", "添加时间"), ("modified", "Date modified", "修改时间"), ("name", "Name", "名称"), ("size", "File size", "文件大小"), ("star", "Rating", "评分"), ("pixels", "Dimensions", "尺寸"))
    sort_select = "".join(option(key, cn if zh else en, sort) for key, en, cn in sorts)
    direction = option("desc", "↓ " + ("降序" if zh else "Descending"), "desc" if descending else "asc") + option("asc", "↑ " + ("升序" if zh else "Ascending"), "desc" if descending else "asc")
    group_names = {"image": ("Images", "图片"), "video": ("Video", "视频"), "audio": ("Audio", "音频"), "document": ("Documents", "文档"), "design": ("Design files", "设计文件"), "font": ("Fonts", "字体")}
    chosen_types = filters.get("type") or []
    types = "".join(
        f'<label class="chip-check"><input type="checkbox" name="type" value="{esc(group)}"{" checked" if group in chosen_types else ""}> {t(*group_names[group])}</label>'
        for group in GROUPS
    )
    star = str(filters.get("star") or "")
    stars = option("", "任意" if zh else "Any", star) + "".join(option(str(n), "★" * n + ("及以上" if zh else " and up") if n < 5 else "★★★★★", star) for n in range(1, 6))
    mode = filters.get("tagmode", "all")
    modes = option("all", "全部包含" if zh else "Has all", mode) + option("any", "包含任一" if zh else "Has any", mode) + option("none", "都不包含" if zh else "Has none", mode)
    added = filters.get("added", "")
    added_names = {"today": ("Today", "今天"), "7d": ("Last 7 days", "7 天内"), "30d": ("Last 30 days", "30 天内"), "365d": ("Last year", "一年内")}
    added_select = option("", "任意时间" if zh else "Any time", added) + "".join(option(key, added_names[key][1 if zh else 0], added) for key in ADDED)
    shape = filters.get("shape", "")
    shape_names = {"landscape": ("Landscape", "横向"), "portrait": ("Portrait", "纵向"), "square": ("Square", "方形")}
    shape_select = option("", "任意" if zh else "Any", shape) + "".join(option(key, shape_names[key][1 if zh else 0], shape) for key in SHAPES)
    colour = filters.get("color", "")
    swatches = f'<label class="swatch-pick any" title="{t("Any colour", "任意颜色")}"><input type="radio" name="color" value=""{" checked" if not colour else ""}><span>∅</span></label>'
    custom = colour and colour not in {code for code, _en, _cn in COLOURS}
    for code, en, cn in COLOURS:
        swatches += f'<label class="swatch-pick" title="{html.escape(cn if zh else en)}"><input type="radio" name="color" value="{code}"{" checked" if code == colour else ""}><span style="background:#{code}"></span></label>'
    if custom:
        swatches += f'<label class="swatch-pick" title="#{esc(colour)}"><input type="radio" name="color" value="{esc(colour)}" checked><span style="background:#{esc(colour)}"></span></label>'

    def number(name: str, value, placeholder: str, step: str = "1") -> str:
        shown = "" if value in (None, "") else ("%g" % value if isinstance(value, float) else str(value))
        return f'<input type="number" name="{name}" value="{esc(shown)}" min="0" step="{step}" placeholder="{esc(placeholder)}" inputmode="decimal">'

    active = describe(filters, lang)
    # The chips show what is set; the panel stays closed so the grid stays in view.
    open_attr = ""
    keep = {"library": library_id, **view}
    if text_query:
        keep["q"] = text_query
    clear = "/ui/library?" + urlencode(keep)
    chips = ""
    if active:
        chips = (
            '<div class="lib-chips">'
            + "".join(f'<span class="chip">{html.escape(label)}</span>' for label in active)
            + f'<a class="chip clear" href="{esc(clear)}">{t("Clear filters", "清除筛选")}</a></div>'
        )
    save = ""
    can_organize = str(chosen.get("role") or "owner") in {"owner", "organize"}
    if (active or text_query) and can_organize and not smart_row:
        saved = as_query(filters)
        save = (
            f'<details class="lib-save-smart"><summary>{t("Save as smart folder", "存为智能文件夹")}</summary>'
            f'<form method="post" action="/ui/library"><input type="hidden" name="csrf" value="{esc(csrf)}">'
            f'<input type="hidden" name="library" value="{esc(library_id)}"><input type="hidden" name="op" value="smart-filters">'
            + "".join(f'<input type="hidden" name="f_{esc(key)}" value="{esc(value)}">' for key, value in saved.items())
            + f'<input type="hidden" name="q" value="{esc(text_query)}">'
            f'<label>{t("Name", "名称")} <input name="name" maxlength="40" required></label>'
            f'<button type="submit">{t("Save", "保存")}</button></form></details>'
        )
    if smart_row:
        chips = f'<div class="lib-chips"><span class="chip">{html.escape(str(smart_row.get("name") or ""))}: {html.escape(str(smart_row.get("text") or ""))}</span></div>' + chips
    return (
        f'<form method="get" action="/ui/library" class="lib-filters" id="lib-filters">{hidden}'
        + '<div class="lib-filter-row">'
        + f'<label class="grow">{html.escape(say(lang, "search_files"))} <input type="search" name="q" value="{esc(text_query)}" maxlength="200"></label>'
        + f'<label>{html.escape(say(lang, "lib_sort"))} <select name="sort">{sort_select}</select></label>'
        + f'<label><span class="sr-only">{t("Direction", "方向")}</span><select name="dir">{direction}</select></label>'
        + f'<button type="submit">{html.escape(say(lang, "search_files"))}</button></div>'
        + f'<details class="lib-filter-panel"{open_attr}><summary>{t("Filters", "筛选")}{(" · " + str(len(active))) if active else ""}</summary>'
        + '<div class="lib-filter-grid">'
        + f'<fieldset><legend>{t("Type", "类型")}</legend>{types}'
        + f'<label>{t("Extension", "扩展名")} <input name="ext" value="{esc(",".join(filters.get("ext") or []))}" placeholder="png, pdf" maxlength="60"></label></fieldset>'
        + f'<fieldset><legend>{t("Rating", "评分")}</legend><select name="star">{stars}</select></fieldset>'
        + f'<fieldset><legend>{t("Tags", "标签")}</legend><input name="tags" value="{esc(", ".join(filters.get("tags") or []))}" list="lib-tag-names" placeholder="a, b" maxlength="200"><select name="tagmode">{modes}</select></fieldset>'
        + f'<fieldset><legend>{t("Date added", "添加时间")}</legend><select name="added">{added_select}</select>'
        + f'<label>{t("From", "从")} <input type="date" name="from" value="{esc(filters.get("from", ""))}"></label><label>{t("To", "到")} <input type="date" name="to" value="{esc(filters.get("to", ""))}"></label></fieldset>'
        + f'<fieldset><legend>{t("File size (MB)", "文件大小（MB）")}</legend>{number("size_min", filters.get("size_min"), "min", "any")} – {number("size_max", filters.get("size_max"), "max", "any")}</fieldset>'
        + f'<fieldset><legend>{t("Dimensions", "尺寸")}</legend><select name="shape">{shape_select}</select>'
        + f'{number("min_w", filters.get("min_w"), t("min width", "最小宽"))} × {number("min_h", filters.get("min_h"), t("min height", "最小高"))}</fieldset>'
        + f'<fieldset class="wide"><legend>{t("Colour", "颜色")}</legend><div class="swatch-row">{swatches}</div></fieldset>'
        + f'</div><p><button type="submit">{t("Apply", "应用")}</button> <a href="{esc(clear)}">{t("Clear filters", "清除筛选")}</a></p></details>'
        + "</form>"
        + chips
        + save
    )


def _swatches(item: dict, base: str) -> str:
    """The main colours, largest first. Each one links to the files that share it."""
    from urllib.parse import quote as _quote

    from ssios.library_palette import hex_rgb

    cells = []
    for row in item.get("palette") or []:
        colour = str(row.get("color") or "") if isinstance(row, dict) else ""
        if hex_rgb(colour) is None or not colour.startswith("#"):
            continue
        share = round(float(row.get("ratio") or 0) * 100)
        cells.append(
            f'<a class="lib-swatch" href="{base}&amp;color={html.escape(_quote(colour[1:]), quote=True)}" title="{html.escape(colour)} · {share}%" '
            f'style="background:{html.escape(colour, quote=True)};flex-grow:{max(1, share)}"><span class="sr-only">{html.escape(colour)}</span></a>'
        )
    return f'<div class="lib-palette">{"".join(cells)}</div>' if cells else ""


def _dims(item: dict, lang: str) -> str:
    from ssios.library_meta import describe

    text = describe(item)
    shape = {"landscape": ("Landscape", "横向"), "portrait": ("Portrait", "纵向"), "square": ("Square", "方形")}.get(str(item.get("orientation") or ""))
    if shape:
        text += " · " + shape[1 if lang == "zh" else 0]
    return text


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{round(size / 1024)} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / (1024 * 1024 * 1024):.1f} GB"


def _hidden(csrf: str, library_id: str, item: str = "") -> str:
    item_html = f'<input type="hidden" name="item" value="{item}">' if item else ""
    return (
        f'<input type="hidden" name="csrf" value="{csrf}">'
        f'<input type="hidden" name="library" value="{html.escape(library_id, quote=True)}">'
        + item_html
    )


def _batch_bar(lang: str, role: str, trash: bool, in_folder: bool) -> str:
    buttons = [
        ("tag", "lib_batch_tag", "tag"),
        ("untag", "lib_batch_untag", "tag"),
        ("folder", "lib_batch_move", "move"),
    ]
    if in_folder:
        buttons.append(("unfolder", "lib_batch_remove_folder", "move"))
    if trash:
        buttons.append(("restore", "lib_batch_restore", "trash"))
        if role == "owner":
            buttons.append(("purge", "lib_batch_purge", "purge"))
    else:
        buttons.append(("trash", "lib_batch_trash", "trash"))
    buttons.extend([
        ("download", "lib_batch_download", "view"),
        ("analyze", "lib_batch_analyze", "tag"),
        ("clear", "lib_batch_clear", "view"),
    ])
    parts = "".join(
        f'<button type="button" data-batch="{action}" data-need="{need}">{html.escape(say(lang, key))}</button>'
        for action, key, need in buttons
    )
    return f'<div id="lib-batch" class="lib-batch" hidden><span data-count></span>{parts}</div>'


def _multi_panel(lang: str) -> str:
    return (
        f'<aside id="lib-multi" class="lib-inspector" hidden data-total="{html.escape(say(lang, "lib_batch_total", size="{size}"), quote=True)}" '
        f'data-none="{html.escape(say(lang, "lib_batch_none"), quote=True)}">'
        f"<h2 data-count></h2><p data-size></p><p>{html.escape(say(lang, 'lib_batch_common'))}</p><div data-tags class=\"lib-chips\"></div></aside>"
    )


def _batch_dialogs(lang: str, folders: list[dict]) -> str:
    options = "".join(
        f'<option value="{html.escape(str(row.get("id") or ""), quote=True)}">{html.escape(str(row.get("name") or ""))}</option>'
        for row in folders
        if isinstance(row, dict)
    )
    return (
        f'<dialog id="lib-tag-dialog" class="lib-dialog"><form method="dialog">'
        f'<label>{html.escape(say(lang, "lib_tag"))} <input name="tag" maxlength="40" list="lib-tag-names" autocomplete="off"></label>'
        f'<button type="submit" data-batch="tag">{html.escape(say(lang, "lib_batch_tag"))}</button>'
        f'<button type="submit" data-batch="untag">{html.escape(say(lang, "lib_batch_untag"))}</button>'
        f'<button type="button" data-close>{html.escape(say(lang, "lib_batch_clear"))}</button></form></dialog>'
        f'<dialog id="lib-folder-dialog" class="lib-dialog"><form method="dialog">'
        f'<label>{html.escape(say(lang, "lib_folder"))} <select name="folder">{options}</select></label>'
        f'<button type="submit" data-batch="folder">{html.escape(say(lang, "lib_batch_move"))}</button>'
        f'<button type="button" data-close>{html.escape(say(lang, "lib_batch_clear"))}</button></form></dialog>'
        f'<dialog id="lib-purge-dialog" class="lib-dialog"><form method="dialog">'
        f'<label>{html.escape(say(lang, "lib_batch_confirm"))} <input name="confirm"></label>'
        f'<button type="submit">{html.escape(say(lang, "lib_batch_purge"))}</button>'
        f'<button type="button" data-close>{html.escape(say(lang, "lib_batch_clear"))}</button></form></dialog>'
    )


def _nav(base: str, lang: str, counts: dict, folder: str, trash: bool, uncategorized: bool, untagged: bool, smart_id: str, suggested: bool = False) -> str:
    links = [
        ("", "lib_all", "all", not folder and not trash and not uncategorized and not untagged and not smart_id and not suggested),
        ("&amp;uncategorized=1", "lib_uncategorized", "uncategorized", uncategorized),
        ("&amp;untagged=1", "lib_untagged", "untagged", untagged),
    ]
    if counts.get("suggested"):
        links.append(("&amp;suggested=1", "lib_suggested", "suggested", suggested))
    links.append(("&amp;trash=1", "lib_trash", "trash", trash))
    return '<nav class="lib-nav">' + "".join(
        f'<a class="{"active" if on else ""}" href="{base}{suffix}">{html.escape(say(lang, key))}<span class="lib-count">{counts[count]}</span></a>'
        for suffix, key, count, on in links
    ) + "</nav>"


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
        return bytes(uploads[0]["bytes"]), str(uploads[0].get("name") or "upload"), source if source in {"paste", "drag", "picker", "folder"} else "picker"
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
        + '<input type="hidden" name="op" value="share">'
        + f'<label>{html.escape(say(lang, "share_with"))} <input name="username" maxlength="32"></label>'
        + f'<label><select name="role">{options}</select></label>'
        + f'<button type="submit">{html.escape(say(lang, "library_shared"))}</button></form>'
    )


def _depth(plane, account_id: str, library_id: str, item: dict) -> str:
    from ssios.reading import depth_of

    return depth_of(plane.accounts.home(account_id), f"file:{library_id}:{item['id']}")


def _analysis_block(selected: dict, lang: str, csrf: str, library_id: str, ident: str, depth: str = "") -> str:
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
    from ssios.reading import depth_label

    forms += (
        f'<form method="post" action="/ui/library"><input type="hidden" name="csrf" value="{csrf}">'
        f'<input type="hidden" name="library" value="{html.escape(library_id, quote=True)}">'
        f'<input type="hidden" name="item" value="{ident}">'
        f'<p><span class="badge">{depth_label(depth)}</span></p>'
        + ('<button name="op" value="mark-read" type="submit">Mark as read</button> ' if depth != "read" else "")
        + '<label>Remind me to read this <select name="when"><option value="tonight">tonight</option><option value="tomorrow" selected>tomorrow morning</option>'
        '<option value="weekend">this weekend</option><option value="week">in a week</option></select></label> '
        '<button name="op" value="remind-read" type="submit">Set reminder</button></form>'
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
        f'<input type="hidden" name="op" value="smart">'
        f'<label>{html.escape(say(lang, "lib_name"))} <input name="name" maxlength="40"></label>'
        f'<label>{html.escape(say(lang, "lib_query"))} <input name="query" maxlength="200" placeholder="{html.escape(say(lang, "smart_query"), quote=True)}"></label>'
        f'<p>{html.escape(say(lang, "smart_query"))}</p>'
        f'<button type="submit">{html.escape(say(lang, "smart_add"))}</button></form>'
    )
    return "".join(links) + form


def _auto_analyze_form(meta: dict, lang: str, csrf: str, library_id: str) -> str:
    checked = " checked" if meta.get("auto_analyze") else ""
    return (
        f'<form method="post" action="/ui/library" class="lib-watch"><input type="hidden" name="csrf" value="{csrf}">'
        f'<input type="hidden" name="library" value="{html.escape(library_id, quote=True)}">'
        f'<input type="hidden" name="op" value="auto_analyze">'
        f'<p>{html.escape(say(lang, "auto_analyze_hint"))}</p>'
        f'<label><input type="checkbox" name="enabled" value="1"{checked}> {html.escape(say(lang, "auto_analyze_enable"))}</label>'
        f'<button type="submit">{html.escape(say(lang, "auto_analyze_saved"))}</button></form>'
    )


def _watch_form(meta: dict, lang: str, csrf: str, library_id: str) -> str:
    watch = meta.get("watch") if isinstance(meta.get("watch"), dict) else {}
    enabled = " checked" if watch.get("enabled") else ""
    delete = " checked" if watch.get("delete_source") else ""
    path = html.escape(str(watch.get("path") or ""), quote=True)
    return (
        f'<form method="post" action="/ui/library"><input type="hidden" name="csrf" value="{csrf}">'
        f'<input type="hidden" name="library" value="{html.escape(library_id, quote=True)}">'
        f'<input type="hidden" name="op" value="watch">'
        f'<p>{html.escape(say(lang, "watch_hint"))}</p>'
        f'<label>{html.escape(say(lang, "watch_folder"))} <input name="path" value="{path}"></label>'
        f'<label><input type="checkbox" name="enabled" value="1"{enabled}> {html.escape(say(lang, "watch_enable"))}</label>'
        f'<label><input type="checkbox" name="delete_source" value="1"{delete}> {html.escape(say(lang, "watch_delete"))}</label>'
        f'<button type="submit">{html.escape(say(lang, "watch_saved"))}</button></form>'
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
    from ssios.library import clean_tags

    folder_id = str(fields.get("folder") or "")
    source = str(fields.get("source") or "drag")
    relative = str(fields.get("relative") or "")
    tags = clean_tags(fields.get("tags") or "")
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
                source=source if source in {"paste", "drag", "picker", "folder"} else "drag",
                peer_ip=peer_ip,
                folder_id=folder_id,
                tags=tags,
            )
        )
    return {"results": results, "summary": drop_summary(lang, results)}


def _language(plane, account_id: str) -> str:
    try:
        lang = plane.skills.account_prefs(account_id).get("language")
    except Exception:
        lang = "en"
    return lang if lang in {"en", "zh"} else "en"


