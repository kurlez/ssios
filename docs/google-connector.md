# Gmail and Drive, read-only

This is the host-side connector for workstream B in `docs/requirements/chat-ui-accounts-google.md`. The chat UI does not live here. It should import this module and pass the account id from the authenticated session.

The no-NIC guest never imports this module, never sees a token, and never opens a socket to Google. Fetched text is untrusted. It is not written into a guest workspace by this module. A VM task may receive it only after the owning account approves that task separately.

## API

```python
from ssios.google_connector import GoogleConnector, member_session, admin_session

connector = plane.google  # or GoogleConnector(plane, store, endpoints)
session = member_session(authenticated_account_id)
```

Build `member_session` only from the login session. Do not take an account id from an admin form and pass it to `connect`, `status`, `disconnect`, `propose_fetch`, or `take_result`. An `admin_session` is rejected by those methods.

| Call | Who | Returns |
| --- | --- | --- |
| `install_client(session, client_id, client_secret)` | member | `{"account_id", "stored": true}`. The secret is stored and not echoed. |
| `connect(session, "drive" or "gmail", scope=None, open_browser=True)` | member | `{"authorization_url", "awaiting_consent": true}`. PKCE loopback on `127.0.0.1`. No token, no verifier. |
| `finish_connect(session, connector)` | member | `status` after the browser returns. |
| `status(session)` | member | exactly `service`, `connected`, `connected_at`. |
| `admin_connection_status(account_id)` | admin console | the same three fields. No Google call and no content. |
| `disconnect(session, connector)` | member | `status` after revoke and deletion of that connector's keychain items. |
| `propose_fetch(session, project_id, tool, arguments, limits)` | member | `{"approval": ...}`. Google is not called. |
| existing `plane.decide(...)` | owning account | approval record only. The mail or file body is not in this response. |
| `take_result(session, approval_id)` | same member | `{"text": ...}` once. Another account, or an admin session, gets `not_found` or `connector_admin_denied`. |
| `revoke_and_delete(account_id)` | account deletion and admin password reset | `{"account_id", "deleted": true, "revoke_ok"}`. Revokes at Google, then deletes the whole `ssios.google.<account_id>` secret service. No token and no content. |

Tokens and the OAuth client secret are stored through the `keyring` package under the service `ssios.google.<account_id>`. That library talks to the macOS Keychain or the Linux Secret Service in-process. Secrets are not written to a file and are not passed on a command line. If `keyring` is missing, SSIOS raises `secure_store_unavailable` and tells you to run `pip install keyring`. One Google identity (`sub`) is kept per SSIOS account. A second consent for a different Google user is revoked and not stored.

Allowed scopes are `https://www.googleapis.com/auth/gmail.readonly`, `https://www.googleapis.com/auth/gmail.metadata`, `https://www.googleapis.com/auth/drive.readonly`, and `https://www.googleapis.com/auth/drive.metadata.readonly`. Anything wider is rejected. `gmail.metadata` cannot request a message body.

Tools are `drive.search`, `drive.read_file`, `gmail.search`, and `gmail.get_message`. Any other name is rejected before a request is sent. Search and read stay inside the approved folder ids, file ids, labels, and dates. Text is prefixed as untrusted external content. HTML is reduced to text. Binaries are not forwarded.

`[connectors] enabled` must list `drive` and/or `gmail`. An empty list denies every fetch.

## Mac setup

Do this on the Mac that runs SSIOS. The browser consent has to be completed by you. Nothing in this repository can approve Google access for you.

1. In Google Cloud Console, create an OAuth client of type Desktop app. Leave the project in Testing and add only the Google account you will use as a test user. Do not publish the app to skip verification.
2. Enable the Gmail API and the Drive API for that project.
3. Install the keyring package, then copy the client id and client secret into the OS keychain. They are not written into the repo or the state directory, and they are not passed to a shell command:

```bash
pip install keyring
```

```bash
PYTHONPATH=src python3 -m ssios oauth-install-client \
  --account YOUR_ACCOUNT_ID \
  --state "$HOME/Library/Application Support/ssios" \
  --config config/ssios.example.toml
```

The prompt reads the secret without echoing it. `YOUR_ACCOUNT_ID` is the household account id from login (letters, digits, `_`, `-`). Use your own id, not someone else's.

4. In your config, under `[connectors]`, set `enabled = ["drive", "gmail"]`. Restart `ssios up` with the same `--state` and `--config` if it is already running.
5. Start consent. A browser opens. Sign in and approve the requested read-only scope yourself:

```bash
PYTHONPATH=src python3 -m ssios oauth-connect \
  --account YOUR_ACCOUNT_ID \
  --connector drive \
  --state "$HOME/Library/Application Support/ssios" \
  --config /path/to/your.toml
```

Repeat with `--connector gmail` if you want mail. The second consent must be the same Google user. A different Google user is refused.

If you click Connect in the chat UI from another computer on the LAN, the redirect still has to hit this Mac's `127.0.0.1`. Finish that consent on the Mac, or run `oauth-connect` here.

6. Check the connection. The output is only the service name, whether it is connected, and when:

```bash
PYTHONPATH=src python3 -m ssios oauth-status \
  --account YOUR_ACCOUNT_ID \
  --state "$HOME/Library/Application Support/ssios" \
  --config /path/to/your.toml
```

7. Read something only after you approve the fetch. This example lists files in one folder. Replace the folder id with one from your own Drive. The command prints the approval and waits. Type `yes` to let the host call Google. The text is marked untrusted.

```bash
PYTHONPATH=src python3 -m ssios connector-fetch \
  --account YOUR_ACCOUNT_ID \
  --project local-summary \
  --tool drive.search \
  --arguments '{"q":"trashed = false"}' \
  --limits '{"folder_ids":["YOUR_FOLDER_ID"],"max_items":5,"max_bytes":4096,"max_calls":20}' \
  --state "$HOME/Library/Application Support/ssios" \
  --config /path/to/your.toml
```

8. Disconnect one service, or wipe the account's Google secrets after a password reset or account deletion:

```bash
PYTHONPATH=src python3 -m ssios oauth-disconnect \
  --account YOUR_ACCOUNT_ID --connector drive \
  --state "$HOME/Library/Application Support/ssios" \
  --config /path/to/your.toml

PYTHONPATH=src python3 -m ssios oauth-revoke-account \
  --account YOUR_ACCOUNT_ID \
  --state "$HOME/Library/Application Support/ssios" \
  --config /path/to/your.toml
```

`oauth-revoke-account` is what account deletion and an admin password reset must call. It revokes the refresh tokens at Google and deletes the keychain items. It does not print them. An admin still cannot read the member's mail or files. The member has to consent again.

The chat UI should show an approval card from `propose_fetch` and call `take_result` only with the member session that owns the approval. Fetched text may be sent to local Ollama. It must not be placed in a microVM unless a separate approval puts it in that account's workspace.
