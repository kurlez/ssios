# SSIOS requirements: Chat UI, household accounts, admin console, Google connector

Source: owner (kurlez) instructions, 2026-11-09, with the Calendar amendment from Wenfeng on 2026-11-10. This document is the acceptance spec for two parallel workstreams:

- **A. Chat UI + accounts + admin** (branch `cursor/ssios-chat-ui`, agent bc-f77b7321)
- **B. Google connector** (branch `cursor/ssios-google-readonly-6121`, agent bc-5a149a1f)

Gmail and Drive stay read-only. Calendar is read/write and is specified in section 9. Neither branch is merged without the owner's explicit request.

## 0. Global constraints (unchanged, apply to everything)

- Public repo: no personal data, secrets, tokens, transcripts, or real mail/file/calendar content in code, tests, fixtures, docs, or logs.
- Never print or commit tokens. Never bypass authentication.
- No cloud model fallback. All model inference via local Ollama (127.0.0.1:11434, native `/api/chat`, `num_ctx` configurable, default 16384).
- Do not loosen any isolation, approval, or security check. No-NIC microVM guarantees stay (nic `["lo"]`, outbound false, marker true, sandbox_denied true, outside_write false).
- Do not delete, move, or migrate `.ssios`, `.ssios-demo`, `.ssios-memfs` (wrap or reference in place).
- Scripts never run `hdiutil`/`diskutil` mutations.
- All code, comments, docs, UI strings, and script output in English.
- No claims of TEE/microVM/Mac readiness without evidence.

## 1. Chat UI and routing (A)

1.1 Web chat UI with streaming responses; per-account conversation history persisted locally under the state dir (never in the repo).
1.2 Router per message:

- Plain conversation / Q&A -> local Ollama directly, no VM.
- Anything that executes code, edits/creates files, runs Codex, or touches a workspace -> start the no-NIC microVM Codex guest (macos-vz/vfkit on macOS, Cloud Hypervisor on Linux).
- When unsure whether it executes or writes -> VM.
- Transparent classifier (rules + optional local-model judgment); decision and reason shown in the UI; user can override (force VM / force direct).

1.3 VM tasks: show live status in chat (booting, running, isolation check results). Results appear as an approval card using the existing approval flow; nothing is applied without explicit approval by the owning account.
1.4 Memory via existing Letta MemFS integration, per account (see 3).

## 2. LAN access (A)

2.1 Opt-in (`ssios ui --lan` or `[ui] bind`); default remains 127.0.0.1.
2.2 When LAN-bound:

- Login required for every route, websocket/SSE.
- HTTPS with a locally generated self-signed cert stored in the state dir; fingerprint printed at startup (mkcert documented as an option).
- Source-address allowlist: private ranges only (RFC1918, link-local, IPv6 ULA) by default; others rejected.
- Strict Host/Origin checks, no CORS, CSRF protection on all state-changing requests (approvals, OAuth connect/disconnect, admin actions).
- Secure HttpOnly SameSite cookies; rate-limited login.

2.3 Print LAN URLs at startup (`<hostname>.local` and IP); document the macOS firewall prompt.
2.4 Document how Google OAuth consent works when "Connect" is clicked from another LAN computer (redirect must complete on the Mac host loopback, or connect must be done on the host).

## 3. Household accounts and per-account isolation (A, with B)

3.1 Web access requires a household account. Roles: admin, member. Admin bootstrapped only via host CLI (`ssios user add --admin`); no default password. Passwords hashed (argon2id or scrypt).
3.2 Strict per-account isolation, enforced server-side (not in UI):

- Own MemFS memory store (separate directory/repo per account).
- Own conversations, tasks, approvals, workspaces; VM tasks mount only that account's workspace/inputs.
- Own Gmail, Drive, and Calendar connections and tokens.
- Every route, router decision, VM task, connector fetch, and memory read/write scoped by the authenticated account id.
- Path-traversal attempts rejected.

3.3 Approvals can only be decided by the owning account.
3.4 Migration: existing single-user state becomes the admin account's data, in place (see 0).

## 4. Admin console (A)

4.1 Registration: sign-up form on the LAN UI and/or one-time invite links/codes created by the admin; new accounts pending until admin approves. Sign-up rate-limited; no self-elevation.
4.2 Admin can: approve/reject sign-ups, enable/disable, reset password (forces change), assign roles, delete accounts (explicit confirmation; documented data retention/archive policy, no silent deletion).
4.3 Admin console requires admin role and the same auth/CSRF/LAN protections.
4.4 **Hard rule: the admin can manage and delete accounts but can never obtain any member's OAuth tokens/refresh tokens/client secrets, nor read or write the content of members' personal cloud accounts (Gmail, Drive, Calendar titles, descriptions, attendees, locations), conversations, or memory.**

- No admin route, API, export, or audit view returns tokens or cloud content. Admin sees only service name, and per connector connected yes/no and connected_at.
- No impersonation feature; connector fetches and calendar writes run only in the owning member's authenticated session.
- Admin password reset revokes the member's sessions AND their Google connections (member must re-consent), so reset-then-login cannot expose mail or calendar content.
- Account deletion: revoke tokens with Google, delete keychain entries, then delete/archive local data per policy; admin never sees token values or content.

## 5. Audit (A)

5.1 Append-only, tamper-evident (hash-chained JSONL in the state dir) with a verify-chain check.
5.2 Events (timestamp, actor account, source IP, action, target, result): login success/failure, sign-ups, account approvals/rejections, role changes, password resets, session revocations, account deletion, Gmail/Drive/Calendar connect/disconnect, each read, and each calendar create/update/delete (service, operation, calendar id hash, event id, time window, counts, approval id only), VM task start/finish/isolation result, task approvals/denials, routing decision summary.
5.3 Never contains message content, memory content, email bodies, file content, calendar titles, descriptions, locations, attendees, tokens, or passwords.
5.4 Admin audit view with filters (user, action, time) and export; members can see their own activity.

## 6. Google connector: Gmail + Drive read-only (B)

6.1 Gmail and Drive stay read-only (`gmail.readonly`, `gmail.metadata`, `drive.readonly`, `drive.metadata.readonly`). Do not widen them when Calendar is connected. Calendar scopes are in section 9.
6.2 Host-side module; the no-NIC guest never receives tokens or network access. The guest cannot call Google. It can only ask the host to propose a connector action.
6.3 OAuth: loopback redirect + PKCE in the user's own browser; the user completes consent. Mac setup guide for creating an OAuth client.
6.4 Secure storage: tokens and the OAuth client secret in OS secure storage through the `keyring` package (macOS Keychain; Linux Secret Service), never plaintext files and never on a process command line. Namespaced per account (`ssios.google.<account_id>`). One Google identity per SSIOS account. If `keyring` is missing, refuse with `secure_store_unavailable`.
6.5 API (member calls take the authenticated account from the session, never an admin-supplied target): `connect`, `status` (`service`, `connected`, `connected_at`, plus per-connector `connectors`), `disconnect` (revoke + keychain delete for that connector), read and calendar-write operations gated by the approval flow, `revoke_and_delete(account_id)` for deletion and admin password reset. No method returns token values. `revoke_and_delete` covers Calendar.
6.6 In chat: requests needing Gmail, Drive, or Calendar data route through the connector with an approval card before any fetch or write. Fetched content goes to local Ollama only; it never enters the VM unless needed for a VM task and approved.
6.7 Documented API so A's adapter can integrate. See `docs/google-connector.md`.

## 7. Tests (must exist and pass)

- Router: direct vs VM decisions, override, default-to-VM on uncertainty.
- LAN: address allowlist, auth on all routes incl. SSE/websocket, CSRF, Origin/Host checks.
- Isolation: account A cannot list/read B's conversations, memory, tasks, approvals, connector status/tokens; path traversal rejected.
- Admin: registration approval flow, role enforcement, cannot retrieve tokens or cloud content via any route, reset-password revokes sessions and Google connections, delete revokes and wipes keychain entries.
- Audit: append-only, hash-chain verification detects tampering, no sensitive content in entries.
- Connector: read-only Gmail/Drive scopes, Calendar scopes in section 9, keychain namespacing, two-account isolation, approval gating, no token in any return value or log, no secret in subprocess argv.
- Existing suite and microVM smoke checks remain green; Mac smoke `scripts/macos-vz-codex-smoke.sh` unaffected.

## 8. Quality gate

- After each major step, self-review against this spec and score code quality 0-100 (correctness vs spec, security, test coverage, readability/structure, docs). Report the score with a short per-dimension breakdown and the evidence (tests run, counts).
- If the score is below 90, or any hard rule (0, 4.4, 6.1, 6.4, 6.5, 9) is not demonstrably met with a test, keep fixing before reporting done.
- Final report: branch, commit, PR URL, test counts, spec checklist, quality score breakdown, and the API the chat UI needs.

## 9. Calendar read/write (B, 2026-11-10)

9.1 Calendar is its own incremental connect. The consent URL requests only:

- `https://www.googleapis.com/auth/calendar.events` (read and write events)
- `https://www.googleapis.com/auth/calendar.calendarlist.readonly` (list calendars)

Do not request `https://www.googleapis.com/auth/calendar`. That scope also deletes calendars and changes sharing, and these two scopes already cover list, get, create, update, and delete of events. A token that comes back with a scope that was not requested is revoked and not stored (`oauth_scope_widened`).

9.2 `admin_connection_status` includes `connectors.calendar.connected` and `connectors.calendar.connected_at`. Connecting Calendar does not mark Gmail or Drive connected, and the reverse is also true.

9.3 Reads: `calendar.list_calendars`, `calendar.list_events`, `calendar.get_event`. `list_events` requires a time window of at most 31 days and a result cap. A missing or longer window raises `connector_window_denied`. Reads go through `propose_fetch` and `take_result` and do not call Google before approval.

9.4 Writes: `calendar.create_event`, `calendar.update_event`, `calendar.delete_event`, one event per approval. The approval action is `connector.write`. The card shows the calendar, title, start, end, time zone, location, and attendees. Update shows before and after. Delete starts with `DESTRUCTIVE`. Nothing is written without that approval. Expired and denied approvals do not write. There is no batch auto-approve.

9.5 `send_updates` defaults to `none`, which is sent as Google's `sendUpdates=none`. Attendees are not emailed unless the approved card shows `all` or `externalOnly`.

9.6 The host connector performs the write after approval. The guest cannot. Create uses a client event id frozen in the approval so a retry cannot create a second event. Update refuses with `connector_conflict` when the current event does not match the approved before-image.

9.7 Audit stores operation, account, calendar id hash, event id, window, count, and approval id. It does not store titles, descriptions, attendees, locations, or tokens. `revoke_and_delete` revokes and deletes Calendar secrets too.
