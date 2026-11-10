# Mac end-to-end test

Run this on the Apple M4 Mac mini. It uses a new state directory and does not read or write `.ssios`, `.ssios-demo`, or `.ssios-memfs`.

```bash
export SSIOS_STATE="$HOME/Library/Application Support/ssios"
mkdir -p "$SSIOS_STATE"
```

The commands below use that directory. If you already keep a config at `.ssios/ollama.toml`, copy it to a new file. Do not pass `--state .ssios`.

```bash
cp config/ssios.example.toml "$SSIOS_STATE/ollama.toml"
```

Edit only the copy. Set `[llm] provider` to `ollama` and `ollama_model` to the local name, usually `qwen2.5-coder:7b`. Under `[connectors]`, set `enabled = ["drive", "gmail", "calendar"]`. Ollama stays on `127.0.0.1:11434`. There is no cloud model fallback.

From a checkout that contains both the chat UI and the Google connector:

```bash
git fetch origin
git checkout cursor/ssios-chat-ui-2e2e
pip install 'keyring>=25'
```

`keyring` talks to the macOS Keychain in-process. If it is missing, connect fails with `secure_store_unavailable`. Secrets are not passed to the `security` command.

## Admin and local UI

```bash
PYTHONPATH=src python3 -m ssios user add --admin --username owner \
  --state "$SSIOS_STATE" --config "$SSIOS_STATE/ollama.toml"
PYTHONPATH=src python3 -m ssios ui \
  --state "$SSIOS_STATE" --config "$SSIOS_STATE/ollama.toml"
```

Type a new password twice. It is not printed. Open the printed `http://127.0.0.1:<port>/ui/login` URL.

- Direct chat: sign in as `owner` and send `What is a microVM?` with route Auto. The badge says `direct`. No guest starts.
- Guest task: send `edit readme.md`. The turn shows booting, running, and the isolation checks, then Approve and Deny. Nothing is applied until you approve.

## Home network

Stop the UI and start it again with `--lan`:

```bash
PYTHONPATH=src python3 -m ssios ui --lan \
  --state "$SSIOS_STATE" --config "$SSIOS_STATE/ollama.toml"
```

The process prints full sign-in URLs: `https://<hostname>.local:<port>/ui/login` and `https://<private-ip>:<port>/ui/login`, plus the certificate SHA-256 fingerprint. Open one of those URLs. A browser that opens the site root is sent to that sign-in page, or to chat if that browser is already signed in. Compare the fingerprint with the browser warning before you continue. macOS may ask to allow incoming connections. Allow it if a browser on another home computer should connect. If you deny it, a browser on this Mac can still use loopback. Only private source addresses are accepted.

## Member, approval, and isolation

On the sign-up page, request an account for `ada` with a password of at least 12 characters. It stays pending. Sign in as `owner`, open Admin, and approve `ada`. Sign out. Sign in as `ada`.

As `ada`, send `remember the fixture code is HARBOR` and approve the memory card. Sign out, sign in as `owner`, and open Memory. That note is not there. `owner` also cannot open `ada`'s chat. An admin password reset or delete does not show `ada`'s mail or files.

## Gmail, Drive, and Calendar

Create a Desktop OAuth client in a Google Cloud project that stays in Testing. Add only your test user. Enable the Gmail API, the Drive API, and the Google Calendar API. Do not publish the app. Calendar consent uses `https://www.googleapis.com/auth/calendar.events` and `https://www.googleapis.com/auth/calendar.calendarlist.readonly`. Do not request the broad `https://www.googleapis.com/auth/calendar` scope.

The account id is the `acct-...` value on the admin row for `ada`, not the username. Install the client for that member. The secret prompt does not echo.

```bash
PYTHONPATH=src python3 -m ssios oauth-install-client \
  --account YOUR_ACCOUNT_ID \
  --state "$SSIOS_STATE" --config "$SSIOS_STATE/ollama.toml"
```

In the UI, signed in as `ada`, open Connections and click Connect gmail, Connect drive, and Connect calendar. Each page shows a consent URL. Open that URL in a browser on this Mac. Google redirects to `127.0.0.1` on this Mac. A browser on another computer cannot finish that redirect. Use the same Google user for all three. A second Google user is refused. After only Calendar is connected, Gmail and Drive still show not connected.

In the config copy, set `[ui] time_zone = "America/Los_Angeles"` and `[llm] ollama_model = "qwen2.5:7b"`. That local model reads Chinese and English calendar sentences. Do not point it at a cloud model. If the model is down, or its JSON is still invalid after one retry, chat says so in English and nothing is read or changed. You can then use the explicit form, for example `list calendar events calendar:primary from:2026-11-10T00:00:00Z to:2026-11-17T00:00:00Z`.

Then, still as `ada`:

- Send `check my inbox`. Approve the card. The model answer stays on this Mac. The approval card appears before any Gmail call.
- Send `list files in drive folder:YOUR_FOLDER_ID`, using a folder id from your own Drive. Approve the card before Drive is called.
- Send `What's on my calendar next week?` or `这周我有什么安排`. The card shows an absolute window in America/Los_Angeles. Approve it before Calendar is called. A window longer than 31 days is refused in English and does not create an approval.
- Send `下周三下午三点和张三开会一小时`. The card shows the resolved Wednesday, 15:00 to 16:00, the time zone, and 张三 in the title or description. It does not invent an email address, and it says attendees are not emailed. Approve once. The page says created and the event id. Approving again does not create a second event.
- Send `把周五的牙医改到下午四点`. The first card is only a read for that Friday. Approve it. If several events match, reply with a number. The second card shows the absolute before and after times. Approving that card is what changes the event. If the event changed in Google after the card was created, the update is not sent.
- Send `取消明天的站会`. Approve the read, then approve the destructive card if it is the right event. Deny it if you want to keep the event. An expired card does nothing.

Sign in as `owner`. Admin shows Gmail, Drive, and Calendar as yes or no, plus connected_at. It does not show message text, file text, event titles, or a token. A second household account cannot see `ada`'s cards either. Only `ada` can approve them.

Reset `ada`'s password from Admin. That revokes `ada`'s sessions and Google connection. `ada` must sign in again and connect again before another read works.

```bash
PYTHONPATH=src python3 -m ssios audit-verify \
  --state "$SSIOS_STATE" --config "$SSIOS_STATE/ollama.toml"
```

The chain check prints `chain ok`. The log has no message bodies, memory text, mail, file contents, tokens, or passwords.

Connector API details that this walkthrough does not repeat are in [google-connector.md](google-connector.md). Chat behavior is in [chat-ui.md](chat-ui.md).
