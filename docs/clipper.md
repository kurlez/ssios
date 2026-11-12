# Site session

SSIOS does not log into a publisher, does not store a password, and does not use an archive mirror. A subscription page is readable only while you keep a site session from the browser where you are already signed in.

Substack and WeChat Official Account pages are fetched the same way. A paid Substack post needs a site session for that exact domain. A WeChat verification page, or a deleted article, is not filled in. Send this page remains the fallback when the fetch cannot see the article.

The cookie stays in the macOS Keychain, one secret per account and per domain. It is not written to disk, not written to the audit log, and not kept in this repository. SSIOS sends it only to that exact host over HTTPS.

Status is on Connections: Active, Expires in N days, or Expired. Three days before expiry, chat and the banner say so. Delete on Connections removes the cookie.

## Setup

1. Sign in to SSIOS and open `/ui/clipper`. Click Generate token and copy it once. SSIOS stores only a hash.
2. Install the extension (below) or the bookmarklet. In the popup, save your SSIOS `https://` address and the clip token.
3. Open the site while you are logged in and click Save site session.

## Renew

1. Click Renew. The site opens in a new tab. Log in if needed, then click Save site session.
2. SSIOS retries the pending clip once. You do not send the URL again.

A 401, a 403, or a page that is still cut off marks the session Expired and keeps that URL for the one retry.

## Extension

The Chrome extension is Manifest V3 and lives in `extensions/ssios-clipper/`. Safari can load the same files as a web extension when the browser allows it. The popup stores the address and token in the browser, not in this repository. Save site session uses the browser cookies API, including HttpOnly cookies, and sends each cookie's expiry.

1. Open `chrome://extensions`.
2. Turn on Developer mode.
3. Choose Load unpacked and select `extensions/ssios-clipper`.
4. Open the popup, save your SSIOS `https://` address and the clip token, then click Save site session on the site.

Send this page is the secondary path. It reads `article` or `main` and posts that HTML. SSIOS sanitizes it again before anything is saved.

## Bookmarklet

The bookmarklet can only read `document.cookie`. If that is empty, it tells you to use the extension, because HttpOnly cookies are invisible to a bookmarklet. Paste remains available on the clipper page if neither path can see the cookie.

Create a bookmark named Save site session. Paste this as the address, then replace the token and the SSIOS address. Use the `https://` address printed by `./scripts/ssios start --lan`.

```text
javascript:(function(){var base="https://127.0.0.1:8443";var token="clip-replace-me";var raw=document.cookie||"";if(!raw||raw.indexOf("=")<0){alert("No cookies were readable. HttpOnly cookies need the SSIOS extension.");return;}fetch(base+"/ui/clip",{method:"POST",headers:{"Content-Type":"application/json","X-SSIOS-Clip-Token":token},body:JSON.stringify({op:"save-session",domain:location.hostname,cookie:raw})}).then(function(r){return r.json()}).then(function(j){var extra="";if(j.retry&&j.retry.status==="saved")extra=" Retried the clip.";else if(j.retry&&j.retry.status==="pending")extra=" Retried the clip. Waiting for your approval.";else if(j.retry&&j.retry.status==="expired")extra=" The clip is still expired.";alert("Saved site session for "+(j.domain||location.hostname)+"."+extra+" HttpOnly cookies are not included. If a clip still looks cut off, use the extension.")}).catch(function(){alert("SSIOS did not accept the site session.")})})();
```

A second bookmark can still send the rendered page. That path does not save a site session:

```text
javascript:(function(){var base="https://127.0.0.1:8443";var token="clip-replace-me";var root=document.querySelector("article")||document.querySelector("main")||document.body;var images=[];root.querySelectorAll("img").forEach(function(img){if(images.length<6&&(img.src||"").indexOf("https://")===0)images.push({src:img.src,alt:img.alt||"image"});});fetch(base+"/ui/clip",{method:"POST",headers:{"Content-Type":"application/json","X-SSIOS-Clip-Token":token},body:JSON.stringify({url:location.href,title:document.title,html:root.innerHTML,images:images})}).then(function(r){return r.json()}).then(function(j){alert(j.status==="saved"?"Saved.":"Waiting for your approval in SSIOS.")}).catch(function(){alert("SSIOS did not accept the clip.")})})();
```

## Paste

On Notes, paste a source URL and the article HTML or text. That path uses your SSIOS session because you are already on the SSIOS page. It still waits for approval unless auto-save is on. A very short page, or a page that says it needs a subscription, is not saved silently.
