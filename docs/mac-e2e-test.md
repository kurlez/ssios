# Mac end-to-end test

Run this on the Apple M4 Mac mini. It uses a new state directory and does not read or write `.ssios`, `.ssios-demo`, or `.ssios-memfs`.

`./scripts/ssios` uses that directory unless `SSIOS_STATE` is already set, and `$SSIOS_STATE/ollama.toml` unless `SSIOS_CONFIG` is set. It does not accept `.ssios`, `.ssios-demo`, or `.ssios-memfs`. If you already keep a config at `.ssios/ollama.toml`, copy the keys you want by hand into the new file. Do not point the script at `.ssios`.

Edit only the copy. These are the same guest settings as `scripts/macos-vz-codex-smoke.sh`, plus the chat model. `[llm] ollama_model` answers chat and reads calendar requests. `[agent.codex] model` is only the model inside the guest. The two names are independent.

```toml
[llm]
provider = "ollama"
ollama_model = "qwen2.5:7b"

[runtime]
backend = "macos-vz"
agent = "codex"
memory_mb = 2048

[agent.codex]
model = "qwen2.5-coder:7b"
num_ctx = 16384

[limits]
task_timeout_seconds = 240

[connectors]
enabled = ["drive", "gmail", "calendar"]

[ui]
time_zone = "America/Los_Angeles"
```

Ollama stays on `127.0.0.1:11434`. There is no cloud model fallback. Pull both local models if they are not already installed: `ollama pull qwen2.5:7b` and `ollama pull qwen2.5-coder:7b`. Install vfkit with `brew install vfkit`.

From a checkout that contains both the chat UI and the Google connector:

```bash
git fetch origin
git checkout cursor/ssios-chat-ui-2e2e
./scripts/ssios setup
```

`setup` creates `.venv` with the checkout's Python, installs `keyring` there, and copies the sample config only when `$SSIOS_STATE/ollama.toml` is missing or empty. `keyring` talks to the macOS Keychain in-process. If it is missing, connect fails with `secure_store_unavailable`. Secrets are not passed to the `security` command. `diagnose` also probes the keychain with a throwaway item named `probe` under the service `ssios.keychain.probe`, then deletes it. A locked keychain, or a start that cannot show UI, reports `keychain_locked` (`-25308`). Unlock the login keychain from Terminal.app on the Mac: `security unlock-keychain ~/Library/Keychains/login.keychain-db`.

## Admin and local UI

```bash
./scripts/ssios user add --admin --username owner
```

Type a new password twice. It is not printed.

Check the guest prerequisites before starting the UI. This does not boot a guest. With the example config still on `dev-guard` and `summary`, the report names those keys and does not pretend the guest is `macos-vz`.

```bash
./scripts/ssios diagnose
```

After the config edits above, prepare the pinned Codex binary and the aarch64 kernel. `vm-prepare` reads `profile` from the config file. It does not accept `--profile`.

```bash
./scripts/ssios vm-prepare
```

Then start the UI from Terminal.app on the Mac, not over SSH. `start` returns after printing the URL. It backgrounds the UI with `nohup`. It does not call `setsid` and it does not use launchd, so the process stays in that login session, but macOS will not show a Keychain prompt for it. Unlock the login keychain before saving a Google client. `status`, `logs`, `restart`, and `stop` use the pid file under `$SSIOS_STATE/run/`. `logs` includes the error class and a traceback when a request fails, with secret values removed.

```bash
./scripts/ssios start
./scripts/ssios status
```

Open the printed `http://127.0.0.1:<port>/ui/login` URL.

- Direct chat: sign in as `owner` and send `What is a microVM?` or `你是谁？你会什么？` with route Auto. The badge says `direct`. No guest starts.
- Guest task: send `edit readme.md`. The turn does not say `Guest: booting` until the prerequisites pass. It then shows booting, running, and the isolation checks, then Approve and Deny. Nothing is applied until you approve. If a prerequisite is still missing, the page names the config keys or the exact `vm-prepare` command above, and it does not show booting.

## Home network

Stop the UI and start it again with `--lan`:

```bash
./scripts/ssios restart --lan
```

The process prints full sign-in URLs: `https://<hostname>.local:<port>/ui/login` and `https://<private-ip>:<port>/ui/login`, plus the certificate SHA-256 fingerprint. Open one of those URLs. A browser that opens the site root is sent to that sign-in page, or to chat if that browser is already signed in. Compare the fingerprint with the browser warning before you continue. macOS may ask to allow incoming connections. Allow it if a browser on another home computer should connect. If you deny it, a browser on this Mac can still use loopback. Only private source addresses are accepted.

## Member, approval, and isolation

On the sign-up page, request an account for `ada` with a password of at least 12 characters. It stays pending. Sign in as `owner`, open Admin, and approve `ada`. Sign out. Sign in as `ada`.

As `ada`, send `remember the fixture code is HARBOR` and approve the memory card. Sign out, sign in as `owner`, and open Memory. That note is not there. `owner` also cannot open `ada`'s chat. An admin password reset or delete does not show `ada`'s mail or files.

## Gmail, Drive, and Calendar

Create a Desktop OAuth client in a Google Cloud project that stays in Testing. Add only your test user. Enable the Gmail API, the Drive API, and the Google Calendar API. Do not publish the app. Calendar consent uses `https://www.googleapis.com/auth/calendar.events` and `https://www.googleapis.com/auth/calendar.calendarlist.readonly`. Do not request the broad `https://www.googleapis.com/auth/calendar` scope.

Install the client for that member. The wrapper adds `--state` and `--config`. Pass the account id from the Admin row. The secret prompt does not echo, and the wrapper does not print it.

```bash
./scripts/ssios oauth-install-client --account kent
```

In the UI, signed in as `ada`, open Connections and click Connect gmail, Connect drive, and Connect calendar. Each page shows one status line and only the Connect button while that service is disconnected. The consent URL is in a field you can copy. Open it in a browser on this Mac. Google redirects to `127.0.0.1` on this Mac. From another computer, paste that full `127.0.0.1` address into Finish consent. The state check stays on the Mac. Use the same Google user for all three. A second Google user is refused. After only Calendar is connected, Gmail and Drive still show not connected. An admin can connect that admin's own account the same way, and still cannot open another account's cards.

In the config copy, set `[ui] time_zone = "America/Los_Angeles"` and `[llm] ollama_model = "qwen2.5:7b"`. That local model reads Chinese and English calendar sentences. Do not point it at a cloud model. If the model is down, or its JSON is still invalid after one retry, chat says so in English and nothing is read or changed. You can then use the explicit form, for example `list calendar events calendar:primary from:2026-11-10T00:00:00Z to:2026-11-17T00:00:00Z`.

Then, still as `ada`:

- Send `check my inbox` or `查一下我的Gmail邮箱里有哪些发件人发给我的邮件`. Gmail is read without an approval card. You see subjects or a sender count from the messages, and an empty inbox says `Inbox, latest 50 — no messages`.
- Send `列一下我的Google Drive的根目录下的文件名` or `list files in My Drive`. Drive lists that folder without asking for a raw folder id. A folder name, recent files, and a file name work the same way.
- Send `What's on my calendar next week?` or `这周我有什么安排`. The reply is a block headed by that week in America/Los_Angeles. An earlier empty day stays as its own line, for example `Nov 11, 2026 — no events`. A window longer than 31 days is refused in English and does not create an approval.
- Send `下周三下午三点和张三开会一小时`. The card shows the resolved Wednesday, 15:00 to 16:00, the time zone, and 张三 in the title or description. It does not invent an email address, and it says attendees are not emailed. Approve once. The page says the event was created. The event id is under Technical details. Approving again does not create a second event.
- Send `把周五的牙医改到下午四点`. The Friday is listed immediately. If several events match, reply with a number. The card shows the absolute before and after times. Approving that card is what changes the event. If the event changed in Google after the card was created, the update is not sent.
- Send `取消明天的站会`. The matching event is listed immediately, then the destructive card asks before anything is deleted. Deny it if you want to keep the event. An expired card does nothing.

Sign in as `owner`. Admin shows Gmail, Drive, and Calendar as yes or no, plus connected_at. It does not show message text, file text, event titles, or a token. A second household account cannot see `ada`'s cards either. Only `ada` can approve them.

Reset `ada`'s password from Admin. That revokes `ada`'s sessions and Google connection. `ada` must sign in again and connect again before another read works.

```bash
./scripts/ssios audit-verify
```

The chain check prints `chain ok`. The log has no message bodies, memory text, mail, file contents, tokens, or passwords.

Connector API details that this walkthrough does not repeat are in [google-connector.md](google-connector.md). Chat behavior is in [chat-ui.md](chat-ui.md).
