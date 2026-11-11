const status = document.getElementById("status");
const baseInput = document.getElementById("base");
const tokenInput = document.getElementById("token");

function store() {
  const api = globalThis.browser || globalThis.chrome;
  return api.storage.local;
}

store().get(["base", "token"]).then((saved) => {
  baseInput.value = saved.base || "";
  tokenInput.value = saved.token || "";
});

document.getElementById("save").addEventListener("click", () => {
  store().set({base: baseInput.value.trim(), token: tokenInput.value.trim()}).then(() => {
    status.textContent = "Saved in this browser.";
  });
});

document.getElementById("send").addEventListener("click", () => {
  const api = globalThis.browser || globalThis.chrome;
  const base = baseInput.value.trim().replace(/\/$/, "");
  const token = tokenInput.value.trim();
  if (!base || !token) {
    status.textContent = "Add the SSIOS address and clip token first.";
    return;
  }
  api.tabs.query({active: true, currentWindow: true}).then((tabs) => {
    const tab = tabs[0];
    if (!tab || !tab.id) {
      status.textContent = "No page is open.";
      return;
    }
    api.scripting.executeScript({target: {tabId: tab.id}, func: extractArticle}).then((results) => {
      const article = results && results[0] && results[0].result;
      if (!article || !article.html) {
        status.textContent = "This page had no article text.";
        return;
      }
      return fetch(base + "/ui/clip", {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-SSIOS-Clip-Token": token},
        body: JSON.stringify(article),
      }).then((response) => response.json()).then((payload) => {
        status.textContent = payload.status === "saved" ? "Saved." : "Waiting for your approval in SSIOS.";
      });
    }).catch(() => {
      status.textContent = "SSIOS did not accept the clip.";
    });
  });
});

function extractArticle() {
  const drop = document.querySelectorAll("script, style, noscript, template");
  drop.forEach((node) => node.remove());
  const root = document.querySelector("article") || document.querySelector("main") || document.body;
  const images = [];
  root.querySelectorAll("img").forEach((img) => {
    if (images.length >= 6) return;
    const src = img.currentSrc || img.src || "";
    if (src.startsWith("https://") || src.startsWith("data:image/")) images.push({src: src, alt: img.alt || "image"});
  });
  return {url: location.href, title: document.title || "", html: root.innerHTML || "", images: images};
}
