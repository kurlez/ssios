# SSIOS

SSIOS is a local household assistant. Chat runs on this computer. A plain question is answered by local Ollama. A request that edits files or a workspace starts a guest with no network interface. Gmail, Drive, and Calendar are read on the host. Calendar changes wait for approval. There is no cloud model fallback.

The day-to-day commands are `./scripts/ssios start|stop|restart|status|logs|diagnose`. The script fills in the state directory and the config. It does not read or write `.ssios`, `.ssios-demo`, or `.ssios-memfs`, and it does not delete them.

## Requirements

- Python 3.11 or newer, and Git 2.x
- [Ollama](https://ollama.com) on `127.0.0.1:11434` when the config provider is `ollama`
- On a Mac, [vfkit](https://github.com/crc-org/vfkit) 0.6.x when chat should start the guest (`brew install vfkit`)

`./scripts/ssios setup` creates `.venv` when it is missing and installs `keyring` there. No other pip packages are required.

## State directory and config

On macOS the state directory is `$HOME/Library/Application Support/ssios`. On Linux it is `$XDG_STATE_HOME/ssios`, or `$HOME/.local/state/ssios` when `XDG_STATE_HOME` is unset. `SSIOS_STATE` overrides that path. `SSIOS_CONFIG` overrides the config file, which otherwise is `$SSIOS_STATE/ollama.toml`.

```bash
./scripts/ssios setup
```

`setup` copies `config/ssios.example.toml` only when that config is missing or empty. Edit the copy. The sample ships with `provider = "mock"` and `ollama_model = "llama3.1:8b"`. For a real local model set:

```toml
[llm]
provider = "ollama"
ollama_model = "qwen2.5:7b"
```

`qwen2.5:7b` is the chat model used for Chinese and English calendar sentences. `[agent.codex] model` is a different name, `qwen3:14b`, and is used only inside the guest. `qwen2.5-coder:7b` and `qwen3-coder:30b` are alternatives. `muse-glimmer:30b-mlx` is a candidate on Apple Silicon and is not the default. Pull models by hand. This program does not pull models and has no telemetry.

```bash
ollama pull qwen2.5:7b
ollama pull qwen3:14b
```

On a Mac set the guest pair in the same file:

```toml
[runtime]
backend = "macos-vz"
agent = "codex"

[ui]
time_zone = "America/Los_Angeles"
```

An empty `time_zone` uses the computer's zone. Set `[connectors] enabled` to the Google services this computer may use. Do not put an OAuth client secret in the file.

## First account

There is no default password. Create the first admin on the host. The username is 3 to 32 characters: a lowercase letter, then lowercase letters, digits, or hyphens. The password is 12 to 200 characters, hashed with scrypt, and not printed. Type it twice.

```bash
./scripts/ssios user add --admin --username owner
```

Other people can request an account at `/ui/signup`. Those accounts stay pending until an admin approves them at `/ui/admin`. `./scripts/ssios user add --username ada` creates an active member directly. An admin cannot read another account's chat, memory, or mail.

## Start, stop, and logs

If this shell is an SSH session, unlock the login keychain before starting. `start` uses `nohup`. It does not call `setsid` and it does not install a launchd agent, so macOS will not show a Keychain dialog. A locked keychain fails later with `keychain_locked` (`-25308`, `errSecInteractionNotAllowed`).

```bash
security unlock-keychain ~/Library/Keychains/login.keychain-db
./scripts/ssios start
./scripts/ssios status
./scripts/ssios logs
./scripts/ssios restart
./scripts/ssios stop
./scripts/ssios diagnose
```

`start` prints the URLs from `<state>/listen.url` and returns. Open that page and sign in. Chat is at `/ui/chat`. `logs` follows `<state>/run/ui.log` and redacts lines that look like secrets. `stop` signals only the process in `<state>/run/ui.pid` after checking that it is this UI. `./scripts/ssios fg` runs the same server in the foreground.

`diagnose` runs `vm-diagnose` and `ui --check`. It prints whether vfkit, the kernel, the initramfs, the Codex binary, and Ollama are present, plus a keychain probe that writes and deletes a throwaway item. It does not boot a guest and does not print tokens.

## Home network

The default bind is `127.0.0.1`. To serve other computers on the home LAN:

```bash
./scripts/ssios restart --lan
```

The server then speaks HTTPS with a self-signed certificate under `<state>/ui/tls/` and prints the certificate fingerprint. Only private source addresses are accepted. The same choice can be saved as `[ui] bind = "0.0.0.0"`. Details are in [docs/chat-ui.md](docs/chat-ui.md).

## VM route

A plain question stays on local Ollama. A request to run code or change a workspace starts the guest named by `[runtime] backend` and `agent`. On a Mac that pair is `macos-vz` and `codex` (vfkit, no virtio-net). On Linux it is `cloud-hypervisor` and `codex`. `dev-guard` does not boot a guest.

```bash
brew install vfkit
./scripts/ssios vm-prepare
./scripts/ssios diagnose
```

`vm-prepare` reads `profile` from the config file. It does not accept `--profile`. It prepares the pinned Codex binary and the aarch64 kernel. After `diagnose` reports the prerequisites present, send a workspace request such as `edit readme.md` from chat. The turn shows booting, the isolation checks, and an approval card. Nothing is applied until that account approves it.

A full boot that does not use the household state directory is `bash scripts/macos-vz-codex-smoke.sh` on Apple Silicon. See [docs/macos-vz.md](docs/macos-vz.md). The Cloud Hypervisor preflight on Ubuntu is [docs/deployment-ubuntu.md](docs/deployment-ubuntu.md). If a prerequisite is missing, the task fails closed and is not run on the host instead.

## Google

Enable the Gmail API, the Google Drive API, and the Google Calendar API in the Cloud project. Leave the app in Testing, add only the Google account you will use as a test user, and create a Desktop OAuth client. Do not publish the app. Connect from the Connections page while signed in, or install the client with:

```bash
./scripts/ssios oauth-install-client --account YOUR_ACCOUNT_ID
```

`YOUR_ACCOUNT_ID` is the id on the Admin row (`acct-` and 16 hex digits). The secret is read from a hidden prompt and stored in the Keychain.

From another computer on the LAN, Connect opens Google in that browser. Google redirects to `http://127.0.0.1:<port>/oauth/callback` on the Mac, which that other browser cannot load. Copy the full redirect address and paste it into Finish consent on the Connections page. The steps, scopes, and the paste flow are in [docs/google-connector.md](docs/google-connector.md).

## Where files live

Paths below are relative to the state directory. The account id is the Admin row, not the username.

| Path | What it is |
| --- | --- |
| `ollama.toml` | The config this script starts with |
| `listen.url` | URLs printed by `start` |
| `run/ui.log` | UI log (`./scripts/ssios logs`) |
| `run/ui.pid` | PID of the UI this script started |
| `accounts/registry.json` | Usernames, roles, and password hashes |
| `accounts/acct-…/` | That account's chat, approvals, and workspace |
| `accounts/acct-…/memory/notes/repo` | That account's MemFS git repository |
| `vaults/acct-…/` | That account's Obsidian vault (notes and clippings) |
| `audit/security.jsonl` | Hash-chained audit log, metadata only |

Memory is one repository per account. Every conversation and every route (local, guest, and Google) reads and writes that same repo. Conversation text stays in the account chat files and is not the memory repository.

The config key `[memory] mode` defaults to `letta-memfs-compat`. A log line with that name is this repository's git markdown library, not the upstream Letta process. `letta-memfs-upstream` is optional and is not used unless the pinned Letta CLI is installed. See [docs/memfs.md](docs/memfs.md).

Inspect memory with git. This prints commit subjects only:

```bash
git -C "$HOME/Library/Application Support/ssios/accounts/acct-0123456789abcdef/memory/notes/repo" log
```

Replace the account id. A new account has a seed commit. Approved memory writes add later commits. `./scripts/ssios audit-verify` checks the audit hash chain. The Memory page in the UI lists the same files for the signed-in account.

## Backup

Stop SSIOS, write one checked archive outside the state directory, and start again. The archive is not encrypted and does not hold Google tokens. Restore unpacks only into a new, empty directory. Details are in [docs/backup.md](docs/backup.md).

```bash
./scripts/ssios stop
./scripts/ssios backup --out "$HOME/ssios-backups"
./scripts/ssios start
```

## Tests

```bash
make test
```

The suite uses a fake Ollama client and a fake guest. It does not call Google and it does not boot vfkit.

## Docs

- [Chat, accounts, and the home-network UI](docs/chat-ui.md)
- [Goals and tasks](docs/tasks.md)
- [Search by meaning and the host tool loop](docs/search.md)
- [Backup and restore](docs/backup.md)
- [Background jobs and start at sign-in](docs/background.md)
- [Release checklist for the Mac](docs/release-checklist.md)
- [Mac end-to-end test](docs/mac-e2e-test.md)
- [Gmail, Drive, and Calendar](docs/google-connector.md)
- [vfkit guest](docs/macos-vz.md)
- [Mac mini M4](docs/macos-m4.md)
- [Architecture and trust boundary](docs/architecture.md)
- [MemFS integration boundary](docs/memfs.md)
- [Implementation status](docs/status.md)
- [Threat model](docs/security.md)
- [Older macOS summary guest](docs/macos-vm.md)
- [Ubuntu and Cloud Hypervisor](docs/deployment-ubuntu.md)
- [Licenses](docs/licenses.md)
- Sample config: [config/ssios.example.toml](config/ssios.example.toml)

## License

Code in this repository uses the MIT license. The Letta MemFS concept and the Letta Code implementation belong to Letta. The upstream license is Apache-2.0. This repository does not copy Letta Code source.
