# Letta MemFS integration boundary

The default mode `letta-memfs-compat` only reuses the public Git Markdown shape so the mock demo can run without the Letta CLI. It is not Letta MemFS.

Household logs that say `letta-memfs-compat` are expected. `config/ssios.example.toml` sets that mode, and `./scripts/ssios setup` copies it. Each account then keeps one git repository at `<state>/accounts/<account-id>/memory/notes/repo`. That repository is shared by every conversation and every route. It is not the upstream Letta process. `letta-memfs-upstream` is optional and fails closed when the pinned Letta CLI is missing. It does not fall back, and this build does not switch the default to upstream.

Only the optional mode `letta-memfs-upstream` calls upstream. The pinned package is `@letta-ai/letta-code@0.34.45`. The integrity digest is in `third_party/letta-code.pin.json`.

## What upstream looks like

Checked on 2026-11-08.

| Source | Observation |
| --- | --- |
| [MemFS docs](https://docs.letta.com/letta-code/memfs) | Memory is a Git repository that belongs to an agent. Root Markdown enters every turn's context. A directory with its own `MEMORY.md` is read on demand. Files other than the index use `name` and `description` front matter. There is no vector index by default. A local agent keeps commits on the current machine. |
| [@letta-ai/letta-code 0.34.45](https://github.com/letta-ai/letta-code) | Apache-2.0. The `bin` in `package.json` is `letta`. The package contains an executable agent, a shell, and a tool runtime. |
| `memory-confinement` exported by `letta-code` | This is a launcher that sandboxes a subprocess. An upstream comment says that process can still read widely on the host. It is not an embeddable MemFS storage SDK. This repository does not call it. |
| [Self-hosting docs](https://docs.letta.com/self-hosting) | `letta --backend local` can run without login and puts state in `~/.letta/lc-local-backend`. Each agent's MemFS is at `memfs/<agent-id>/memory`. Agent tools run on the machine that hosts the harness. |
| [Local MemFS on the Letta server](https://docs.letta.com/v1-sdk/docker) | `LETTA_MEMFS_SERVICE_URL=local` provides API-level memory blocks. The docs say the Git HTTP proxy returns 501 in that mode, so Letta Code's clone/push flow is unavailable. The community Git sidecar is marked unofficial. |

The npm package `memfs` is a different in-memory filesystem package. This project does not depend on it, and it does not depend on `@letta-ai/letta-code` or `@letta-ai/letta-code-sdk`.

## What this repository actually does

The control plane owns an ordinary Git repository whose directory shape matches the documented `$MEMORY_DIR`:

- Root `MEMORY.md` is an index with no front matter and belongs to core memory
- Other root Markdown files have `name` and `description` and are loaded before each task
- `topics/` and `skills/` do not enter core and can be read only by path
- Search is a case-insensitive keyword scan, `search_mode=keyword`
- An approved change is fixed with `git commit`. The expected revision must equal the current HEAD
- Forget uses `git rm` and changes the source record to a tombstone. The API no longer returns the body

Broker API:

- `GET /v1/memory/core`
- `GET /v1/memory/file`
- `GET /v1/memory/search`
- `POST /v1/memory/propose`
- `POST /v1/memory/forget`
- Approval goes through `POST /v1/approvals/<id>/decide`, and the body includes `expected_revision`

A proposal is written to a JSON file outside the repository first, with trust `untrusted`. Before approval, core reads and path reads cannot see the proposal body.

`ssios memfs-info` only prints the pinned version above and the local probe result. It does not run `letta` by default. `--execute-version` is what runs `letta --version`, and it sets `LETTA_CODE_TELEM=0` and `DISABLE_AUTOUPDATER=1`. The control plane does not do this at startup.

## Commands the upstream mode actually runs

Inside an empty `HOME` and a separate `LETTA_LOCAL_BACKEND_DIR`, only these arguments are allowed:

- `letta --version`
- `letta --backend local agents create --personality blank`
- `letta --backend local memory tokens --format json`
- `letta --backend local memory export --agent ... --out ...`
- `letta --backend local memory status --agent ...`

The `memory` subcommand help says memory is backed by Git and commits use git. After approval, SSIOS only runs `git add` / `git commit` in that agent's `memfs/<agent-id>/memory` directory, and it points `core.hooksPath` at an empty directory so Letta's installed `post-commit` does not run. That hook would `git push` in the background when a remote is configured.

The core file list comes from `memory tokens`. Topic directories come from `MEMORY.md` files in `memory export` that are not on that list. On read, the bytes at Git `HEAD` must match the same export.

Arguments that are explicitly rejected include `-p`, `--yolo`, `--permission-mode`, `--tools`, and `--disable-memory-guard`. There is therefore no full agent turn without a model. Creating an agent on the upstream local backend itself needs no account and no model call. That was verified on 0.34.45.

## What is not done

- `letta -p` is not run, and neither is any turn that would let the harness execute a shell
- `api.letta.com` is not called
- A Git smart HTTP sidecar is not implemented
- There is no QMD
- `memory-confinement` is not treated as a security boundary
- Compatibility mode does not take over automatically when upstream mode fails
