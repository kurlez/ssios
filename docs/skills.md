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

## Installed skills

Besides the built-in web skills, an account can install skills in the Agent Skills layout: a folder with a `SKILL.md` whose header has a `name` and a `description`, plus optional reference files and scripts. Open **Skills** from the account menu.

**Install from a link.** Paste a GitHub folder (`https://github.com/owner/repo/tree/main/skills/name`), a GitHub `SKILL.md` page, a `.zip`, or a plain `SKILL.md`. The host broker fetches it with the same public-only rules as other web reads, so nothing is fetched while an admin has web reads off. The archive is unpacked into a staging folder: links, paths that leave the folder, files over 2 MB, skills over 10 MB, and archives that unpack suspiciously are refused. Nothing is installed until the owner reads the safety report and confirms.

**Safety check** (`skill_scan.py`). Every file is read, nothing is run. Each finding names the file and line:

- Instructions that work against the user (ignore earlier instructions, hide something from the user, send passwords or notes to an address), invisible characters, and comments a reader would not see. These refuse the skill. The same words inside quotation marks, usually an example of what not to write, are a warning instead.
- Scripts that read keys, passwords, or browser data, stay on the computer (cron, launchd, shell profiles), delete outside their folder, or run hidden code. These refuse the skill.
- Scripts that use the network, start other programs, install software, or touch paths outside their work folder. These are warnings: the owner must tick "I read the warnings" to install. A comment in a script and an example in Markdown are notes.
- Compiled programs refuse the skill. Unknown file types are noted.

Against the 19 published skills in `anthropics/skills`, 12 pass, 7 have warnings (their scripts call other programs or open sockets), and none is refused.

**How the local model uses a skill.** The planner's system prompt lists each enabled skill's name and description. When a request matches, the model calls `skills_read` for the skill's `SKILL.md` (and, if it needs them, its reference files) and follows the steps with the tools it already has. A skill never adds a tool or skips an approval.

**Scripts run only in a sandbox.** Only an admin can allow a skill's scripts, and only on a computer with a sandbox. The model can then call `skills_run`, which creates an approval like any other write. When the owner approves, the script runs:

- macOS: `sandbox-exec` with a deny-by-default profile: read system libraries and the skill, write only an empty work folder, no network.
- Linux: `bwrap` (bubblewrap) with every namespace unshared, the skill read-only at `/skill`, an empty `/work`, no network.
- No environment from the account, no home folder, 60 seconds, 64 KB of output, CPU and file-size limits, and every process stopped at the end.
- Without either sandbox, scripts do not run. There is no fallback.

The output is shown on the approval page and posted in the conversation the run came from. Files the script writes are listed and not kept. Fetch, install, script permission changes, runs, and removal are in the security audit log.

Not done yet: handing Files items to a script as input, and keeping what a script writes.
