# Browser clipper

SSIOS does not log into a publisher and does not use an archive mirror. A clip comes from the browser where you are already signed in. The extension or bookmarklet reads the rendered page and posts it to your SSIOS computer.

The request uses a clip token in the `X-SSIOS-Clip-Token` header. It does not send the SSIOS session cookie. Generate the token at `/ui/clipper` while you are signed in. Copy it once. Revoke it there when you are done. SSIOS stores only a hash of the token.

Auto-save on the Settings page writes the clip immediately and keeps one Undo. Otherwise the clip waits for the usual approval. A very short page, or a page that says it needs a subscription, is not saved silently. SSIOS asks you to approve the partial clip.

## Bookmarklet

Create a bookmark named Send to SSIOS. Paste this as the address, then replace the token and the SSIOS address:

```text
javascript:(function(){var base="https://127.0.0.1:8443";var token="clip-replace-me";var root=document.querySelector("article")||document.querySelector("main")||document.body;var images=[];root.querySelectorAll("img").forEach(function(img){if(images.length<6&&(img.src||"").indexOf("https://")===0)images.push({src:img.src,alt:img.alt||"image"});});fetch(base+"/ui/clip",{method:"POST",headers:{"Content-Type":"application/json","X-SSIOS-Clip-Token":token},body:JSON.stringify({url:location.href,title:document.title,html:root.innerHTML,images:images})}).then(function(r){return r.json()}).then(function(j){alert(j.status==="saved"?"Saved.":"Waiting for your approval in SSIOS.")}).catch(function(){alert("SSIOS did not accept the clip.")})})();
```

Use the `https://` address printed by `./scripts/ssios start --lan`. A bookmarklet runs in the page, so the publisher page must be allowed to call your SSIOS address. The extension below is the more reliable install.

## Extension

The Chrome extension is Manifest V3 and lives in `extensions/ssios-clipper/`. Safari can load the same files as a web extension when the browser allows it. The popup stores the address and token in the browser, not in this repository.

1. Open `chrome://extensions`.
2. Turn on Developer mode.
3. Choose Load unpacked and select `extensions/ssios-clipper`.
4. Open the popup, save your SSIOS `https://` address and the clip token, then open an article and click Send this page.

The extension reads `article` or `main`, drops scripts, and posts the HTML plus up to six image URLs. SSIOS sanitizes the HTML again before anything is saved.

## Site session

A site session is off until you add one domain on the clipper page. Paste a cookie header for that exact domain, for example `nytimes.com`. SSIOS keeps the cookie in the OS secret store and keeps only the domain, last-used time, and expiry in its files. It sends the cookie only to that host over HTTPS. Delete removes it. A later 401 or 403 says "Site session expired, refresh it." SSIOS does not store a password and does not log in for you.

## Paste

On Notes, paste a source URL and the article HTML or text. That path uses your SSIOS session because you are already on the SSIOS page. It still waits for approval unless auto-save is on.
