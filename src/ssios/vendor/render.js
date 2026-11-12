/* SSIOS Markdown renderer. User text is escaped. Only the tags below are emitted. */
(function (root) {
  "use strict";

  function esc(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function safeUrl(url) {
    var text = String(url || "").trim();
    if (!text || /^\s*javascript:/i.test(text) || /^\s*data:/i.test(text)) return "";
    if (/^https?:\/\//i.test(text) || /^mailto:/i.test(text)) return text;
    return "";
  }

  function extractMath(input) {
    var s = String(input);
    var out = [];
    var i = 0;
    function pushText(value) {
      if (!value) return;
      if (out.length && out[out.length - 1].type === "text") out[out.length - 1].value += value;
      else out.push({ type: "text", value: value });
    }
    while (i < s.length) {
      if (s.slice(i, i + 2) === "$$") {
        var end = s.indexOf("$$", i + 2);
        if (end < 0) { pushText(s.slice(i)); break; }
        out.push({ type: "math", display: true, value: s.slice(i + 2, end) });
        i = end + 2;
        continue;
      }
      if (s.slice(i, i + 2) === "\\[" || s.slice(i, i + 2) === "\\(") {
        var close = s.slice(i, i + 2) === "\\[" ? "\\]" : "\\)";
        var at = s.indexOf(close, i + 2);
        if (at < 0) { pushText(s.slice(i)); break; }
        out.push({ type: "math", display: close === "\\]", value: s.slice(i + 2, at) });
        i = at + 2;
        continue;
      }
      if (s.charAt(i) === "$" && s.charAt(i + 1) !== "$") {
        var money = /^\$(\d{1,3}(?:,\d{3})*|\d+)(\.\d+)?/.exec(s.slice(i));
        if (money) {
          var after = i + money[0].length;
          if (s.charAt(after) !== "$") {
            pushText(money[0]);
            i = after;
            continue;
          }
        }
        if (s.charAt(i + 1) === " " || s.charAt(i + 1) === "\n" || s.charAt(i + 1) === "") {
          pushText("$");
          i += 1;
          continue;
        }
        var closeDollar = -1;
        for (var k = i + 1; k < s.length; k++) {
          if (s.charAt(k) === "\n") break;
          if (s.charAt(k) === "$" && s.charAt(k - 1) !== "\\") { closeDollar = k; break; }
        }
        if (closeDollar > i + 1 && s.charAt(closeDollar - 1) !== " ") {
          out.push({ type: "math", display: false, value: s.slice(i + 1, closeDollar) });
          i = closeDollar + 1;
          continue;
        }
      }
      var next = s.length;
      for (var p = i; p < s.length; p++) {
        var two = s.slice(p, p + 2);
        if (two === "$$" || two === "\\[" || two === "\\(" || s.charAt(p) === "$") { next = p; break; }
      }
      if (next === i) { pushText(s.charAt(i)); i += 1; }
      else { pushText(s.slice(i, next)); i = next; }
    }
    return out;
  }

  function mathSpan(value, display) {
    return '<span class="math" data-display="' + (display ? "1" : "0") + '">' + esc(value) + "</span>";
  }

  function inline(text) {
    var html = "";
    var rest = String(text || "");
    var pattern = /(`[^`\n]+`)|(\*\*([^*]+)\*\*)|(\[([^\]]+)\]\(([^)\s]+)\))/;
    while (rest) {
      var pieces = extractMath(rest);
      if (pieces.length === 1 && pieces[0].type === "math") {
        html += mathSpan(pieces[0].value, pieces[0].display);
        break;
      }
      var chunk = pieces[0] && pieces[0].type === "text" ? pieces[0].value : rest;
      if (pieces[0] && pieces[0].type === "math") {
        html += mathSpan(pieces[0].value, pieces[0].display);
        rest = rest.slice(pieces[0].value.length + (pieces[0].display && rest.slice(0, 2) === "$$" ? 4 : 2));
        continue;
      }
      var match = pattern.exec(chunk);
      if (!match) {
        html += esc(chunk);
        rest = rest.slice(chunk.length);
        continue;
      }
      html += esc(chunk.slice(0, match.index));
      var token = match[0];
      if (token.charAt(0) === "`") html += "<code>" + esc(token.slice(1, -1)) + "</code>";
      else if (token.slice(0, 2) === "**") html += "<strong>" + inline(token.slice(2, -2)) + "</strong>";
      else {
        var href = safeUrl(match[6]);
        if (href) html += '<a href="' + esc(href) + '" rel="noopener noreferrer">' + esc(match[5]) + "</a>";
        else html += esc(match[5]);
      }
      rest = rest.slice(chunk.length - (chunk.length - (match.index + token.length)) + (rest.length - chunk.length));
      rest = rest.slice(0, 0) + String(text).slice(String(text).length - rest.length);
    }
    return html;
  }

  function inlineParts(text) {
    var html = "";
    extractMath(text).forEach(function (part) {
      if (part.type === "math") html += mathSpan(part.value, part.display);
      else html += formatText(part.value);
    });
    return html;
  }

  function formatText(text) {
    var html = "";
    var rest = String(text || "");
    var pattern = /(`[^`\n]+`)|(\*\*([^*]+)\*\*)|(\[([^\]]+)\]\(([^)\s]+)\))/;
    while (rest) {
      var match = pattern.exec(rest);
      if (!match) { html += esc(rest); break; }
      html += esc(rest.slice(0, match.index));
      var token = match[0];
      if (token.charAt(0) === "`") html += "<code>" + esc(token.slice(1, -1)) + "</code>";
      else if (token.slice(0, 2) === "**") html += "<strong>" + esc(token.slice(2, -2)) + "</strong>";
      else {
        var href = safeUrl(match[6]);
        if (href) html += '<a href="' + esc(href) + '" rel="noopener noreferrer">' + esc(match[5]) + "</a>";
        else html += esc(match[5]);
      }
      rest = rest.slice(match.index + token.length);
    }
    return html;
  }

  function highlight(code) {
    var keywords = /^(if|else|for|while|return|def|class|import|from|function|const|let|var|true|false|null|None|and|or|not|in|of)$/;
    var html = "";
    var i = 0;
    while (i < code.length) {
      if (code.charAt(i) === "#") {
        var end = code.indexOf("\n", i);
        if (end < 0) end = code.length;
        html += '<span class="hl-com">' + esc(code.slice(i, end)) + "</span>";
        i = end;
        continue;
      }
      if (code.slice(i, i + 2) === "//") {
        var end2 = code.indexOf("\n", i);
        if (end2 < 0) end2 = code.length;
        html += '<span class="hl-com">' + esc(code.slice(i, end2)) + "</span>";
        i = end2;
        continue;
      }
      var quote = code.charAt(i);
      if (quote === '"' || quote === "'") {
        var j = i + 1;
        while (j < code.length && code.charAt(j) !== quote && code.charAt(j) !== "\n") {
          if (code.charAt(j) === "\\") j += 1;
          j += 1;
        }
        if (code.charAt(j) === quote) j += 1;
        html += '<span class="hl-str">' + esc(code.slice(i, j)) + "</span>";
        i = j;
        continue;
      }
      if (/[A-Za-z_]/.test(code.charAt(i))) {
        var k = i + 1;
        while (k < code.length && /[A-Za-z0-9_]/.test(code.charAt(k))) k += 1;
        var word = code.slice(i, k);
        html += keywords.test(word) ? '<span class="hl-kw">' + esc(word) + "</span>" : esc(word);
        i = k;
        continue;
      }
      html += esc(code.charAt(i));
      i += 1;
    }
    return html;
  }

  function isList(line) {
    return /^(\s*)([-*+]|\d+\.)\s+/.test(line);
  }

  function renderList(lines) {
    var ordered = /^\s*\d+\./.test(lines[0]);
    var tag = ordered ? "ol" : "ul";
    var html = "<" + tag + ">";
    lines.forEach(function (line) {
      var body = line.replace(/^(\s*)([-*+]|\d+\.)\s+/, "");
      var task = /^\[( |x|X)\]\s+/.exec(body);
      if (task) {
        var done = task[1].toLowerCase() === "x";
        var checked = done ? " checked" : "";
        html += '<li class="task' + (done ? " done" : "") + '"><input type="checkbox" disabled title="This box is read-only."' + checked + "> " + inlineParts(body.slice(task[0].length)) + "</li>";
      } else html += "<li>" + inlineParts(body) + "</li>";
    });
    return html + "</" + tag + ">";
  }

  function renderTable(lines) {
    function cells(line) {
      return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(function (cell) { return cell.trim(); });
    }
    var html = "<table>";
    lines.forEach(function (line, index) {
      if (index === 1 && /^\|?\s*:?-{3,}/.test(line)) return;
      var tag = index === 0 ? "th" : "td";
      html += "<tr>" + cells(line).map(function (cell) { return "<" + tag + ">" + inlineParts(cell) + "</" + tag + ">"; }).join("") + "</tr>";
    });
    return html + "</table>";
  }

  function render(src) {
    var lines = String(src || "").replace(/\r\n/g, "\n").split("\n");
    var html = "";
    var i = 0;
    while (i < lines.length) {
      var line = lines[i];
      if (!line.trim()) { i += 1; continue; }
      if (line.indexOf("```") === 0) {
        var lang = line.slice(3).trim();
        var code = [];
        i += 1;
        while (i < lines.length && lines[i].indexOf("```") !== 0) { code.push(lines[i]); i += 1; }
        if (i < lines.length) i += 1;
        html += '<div class="codeblock"><div class="codehead"><span>' + esc(lang || "code") + '</span></div><pre><code>' + highlight(code.join("\n")) + "</code></pre></div>";
        continue;
      }
      if (/^#{1,6}\s+/.test(line)) {
        var level = line.match(/^#+/)[0].length;
        html += "<h" + level + ">" + inlineParts(line.replace(/^#{1,6}\s+/, "")) + "</h" + level + ">";
        i += 1;
        continue;
      }
      if (/^>\s?/.test(line)) {
        var quote = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) { quote.push(lines[i].replace(/^>\s?/, "")); i += 1; }
        html += "<blockquote>" + quote.map(inlineParts).join("<br>") + "</blockquote>";
        continue;
      }
      if (/^\s*\|/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1])) {
        var table = [];
        while (i < lines.length && /^\s*\|/.test(lines[i])) { table.push(lines[i]); i += 1; }
        html += renderTable(table);
        continue;
      }
      if (isList(line)) {
        var items = [];
        while (i < lines.length && (isList(lines[i]) || (!lines[i].trim() && i + 1 < lines.length && isList(lines[i + 1])))) {
          if (lines[i].trim()) items.push(lines[i]);
          i += 1;
        }
        html += renderList(items);
        continue;
      }
      var para = [];
      while (i < lines.length && lines[i].trim() && !/^#{1,6}\s+/.test(lines[i]) && lines[i].indexOf("```") !== 0 && !isList(lines[i]) && !/^>\s?/.test(lines[i])) {
        para.push(lines[i]);
        i += 1;
      }
      html += "<p>" + para.map(inlineParts).join("<br>") + "</p>";
    }
    return html;
  }

  root.ssiosRender = render;
})(typeof globalThis !== "undefined" ? globalThis : this);
