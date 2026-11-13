# Web skills

Web skills read the public internet on the computer that runs SSIOS. The guest stays offline. The model does not get a socket. The same host broker used for Google connectors performs every request. A page you are already reading can be sent from your browser; see [clipper.md](clipper.md).

Skills are declarative files under `skills/<name>/`:

- `SKILL.md` describes the skill and gives Chinese and English examples.
- `manifest.toml` names the skill, the allowed HTTP methods, the allowed domains, the tools, and the size and time limits.

Two skills are on when an admin enables web skills:

- `weather` calls the keyless Open-Meteo geocoding and forecast APIs. When this computer cannot reach them, it asks `api.met.no` for the conditions at those coordinates and `wttr.in` to find the place by name. Both are keyless. A host that just failed is skipped for ten minutes so the timeout is not waited out on every question. Units are metric. A city in the message wins. Otherwise SSIOS uses the default city saved in Settings.
- `web_fetch` fetches one `http` or `https` URL from the message with `GET`. It removes scripts and shows the title, the site, a short extract, and a link.

`web_search` is present and disabled. There is no keyless search provider in this build, and SSIOS does not call a cloud model to search. A future provider implements `search(query)` and returns public `https` URLs. Those URLs still pass through this broker.

## Policy

`[skills] enabled` in the config starts off. An admin can turn skills on or off from the admin page. That switch is stored in the state directory, not in the git repository. Each account can turn `weather` or `web_fetch` off in Settings and can save a default city.

`allow_domains` restricts every skill to those hosts when the list is not empty. `deny_domains` always blocks those hosts. Weather calls `api.open-meteo.com` and `geocoding-api.open-meteo.com`, and `api.met.no` and `wttr.in` only when those two cannot be reached. The place name or its coordinates go to whichever service answers.

Only `GET` is implemented. A request body and an `Authorization` header are not sent. A site session cookie, if you saved one, goes only to that exact host. SSIOS resolves the name and refuses loopback, private, link-local, and carrier-grade NAT addresses. It checks again after each redirect. The audit line stores the host, the HTTP status, and the byte count. It does not store the page or the city.

Fetched text is labeled untrusted. It is not parsed as a tool call, and it cannot approve a connector write or start the guest.

## Chat

`What's the weather in Seattle?` and `今天天气怎么样` render a weather card. A message that contains an `https://` address renders an article card. The guest is not started.

## Substack and WeChat

A Substack post, including a custom domain whose page identifies itself as Substack, and an `mp.weixin.qq.com` article are read for the title, the author or account name, the publish date, and the article text. WeChat text comes from `js_content`. Its images use `data-src`.

A public Substack post is a normal GET. A paid post is read only when this account already has a site session for that exact host. SSIOS does not log in, does not store a password, and does not fetch an archive mirror.

If the post needs a subscription, the reply asks you to save a site session or use the browser clipper. If WeChat asks for a verification check, or to open the WeChat app, the reply says to open the page and use the clipper. If the article was deleted, the reply says so. Nothing is invented.
