# Implementation status

Status is in three groups. A command that builds and a preflight that passes do not mean a microVM has booted.

## Tested

Covered by `make test` on Linux x86_64, Python 3.12, and Git 2.43:

- Health, tasks, core memory, on-demand topics, staging, human approval, publish, and read-after-restart on the mock dev profile
- Unauthenticated requests, a bad token, a worker token that cannot approve, cross-project denial, and an export path that was not granted
- Approval tampering, replay, expiry, a changed payload, and a frozen stage
- Path traversal, symlinks, export manifest hashes, and Git files with no recorded source
- CAS for memory and publish, including concurrent approvals where only one revision wins
- After forget, the worktree and search no longer return the body, while `git log -p` still keeps the phrase
- Core token budget truncation
- Task timeout, human cancel, and the output length cap
- The model's `approve` field is ignored
- The shell tool is rejected. The only process actually started is Git, not a shell
- The Ollama adapter's `/api/chat` request against a loopback stub, and `llm_unavailable` when `127.0.0.1:9` cannot be reached
- The production-profile CLI exits fail-closed before creating tokens
- Choosing the `cloud-hypervisor` or `macos-vm` backend fails closed on task creation and does not run on the host. `macos-vm` only checks whether `krunvm` is on PATH. This machine has not booted a guest
- `ssios gc --dry-run` only lists one-shot export copies under `upstream-letta/exports/<agent>/`. A real delete requires typing `yes` again, and it refuses to delete Git memory under `backend/memfs`
- The local `/ui` page: after login the session cookie is HttpOnly and SameSite=Strict, every POST checks CSRF, and the approve button carries the same payload hash, revision, and bound hash. `<script>` in a note is escaped. The human token is not written to the session file
- The snapshot policy rejects a vault. A clean task directory also returns unimplemented and copies no files
- Token file permissions, and refusal to start after those permissions are widened
- The source contains no `shell=True` and no `os.system`
- The internal Git repository does not inherit external signing, hooks, identity, attributes, or `*.md` exclude rules. Committing approved content that already exists does not report an empty `git command failed`. This is a Linux regression, not a measurement on a Mac
- Pinned Letta Code 0.34.45: `agents create --backend local --personality blank` needs no account or model and creates a Git repository at `memfs/<agent-id>/memory`. `memory tokens` marks root Markdown as core. An approved git commit can be read back with `memory export`, and a new topic does not enter the core list. That test is enabled by `SSIOS_LETTA_UPSTREAM=1`. Default CI does not install the package
- `ssios summary` connects to an already running service with a relative `--state`. Subprocess tests cover `yes`, `no`, EOF, and a retry after one invalid output that creates a new task and keeps the failure record. `approve` inside model `<think>` and `message.thinking` is not parsed as a command. The Ollama adapter sends a JSON schema and, if the service rejects the schema, tries `format: "json"` only once more. These interactions use a loopback HTTP stub. `ssios ingest` writes md/txt notes to `topics/<project>/` after approval. `summary --topic` summarizes only those bodies. A thinking model is asked for prose first. JSON is used only when it validates. The failed raw output is written to `diagnostics/<task_id>.json` (0600) in the state directory and is not auto-approved. On Linux, official Ollama 0.40.15 was tested with the public `qwen2.5:0.5b`, `qwen3:0.6b`, and `deepseek-r1:1.5b`. `deepseek-r1:7b` was not run here on a Mac. With `SSIOS_LETTA_UPSTREAM=1`, the same command commits the topic into the pinned Letta 0.34.45 local MemFS, and that topic does not enter core

The provider name used by mock tests is `mock`. The name of the Ollama stub test says the other side is not a real Ollama daemon.

## Depends on an external integration

These code paths exist, but this environment has not run them as an end-to-end system:

- A real Ollama process and a real 7B/8B model. The adapter sends `POST /api/chat`. When this status was written, `127.0.0.1:11434` on this Linux machine had no service, so this does not claim that `deepseek-r1:7b` ran
- The default tests do not download a guest kernel. When `kernel_path` in the sample config is empty, a `cloud-hypervisor` task still fails closed and does not run on the host instead
- After `/dev/kvm` was opened for the current user on this machine, Cloud Hypervisor v54.0 and the Linux 7.2.8 kernel from the cloud-hypervisor release (`bzImage-x86_64` of `ch-release-v7.2.8-20261009`) booted an initramfs. `echo ok` took 898 ms of wall time. The summary worker with virtio-fs input and scratch and no `--net` took 1576 ms. The task reached `awaiting_approval` with `isolated` true. The model call was the host mock. krunvm is not installed, so the macOS path was not started
- `[runtime] agent = "codex"` ran the pinned `codex-cli 0.167.0` musl binary on the same Cloud Hypervisor. The host checks the SHA256 in `third_party/codex.pin.json` first. The guest does not download. The guest has only `lo`. After `ip addr` assigns `127.0.0.1/8`, the loopback relay reaches the host's mock Responses service over vsock. There is no `--net`. `/sys/class/net` contains only `lo`, and a connection to `1.1.1.1:443` fails. One `POST /v1/responses` was recorded by the proxy. The task reached `awaiting_approval` with `isolated` true and `boot_ms` 7155. The last message was the mock summary and was not written to MemFS. Codex's standard error was empty. This is not a Mac result. `scripts/macos-codex-smoke.sh` only checks the aarch64 package on Apple Silicon. It does not boot a guest and does not treat that check as isolation success
- `macos-vz` uses vfkit 0.6.4. When the user ran `a982e34` on an M4, the task reached `awaiting_approval` with `isolated` true, `boot_ms` 22209, only `lo` as the NIC, and a preflight context of 16384. The model did not write the marker and did not write outside the workspace, so `marker` and `sandbox_denied` were both false. The guest now writes the marker and attempts the outside write inside the same `workspace-write` sandbox after Codex. A missing marker is `probe_marker_missing`. A missing denial is `probe_sandbox_not_denied`. This Linux machine did not start Virtualization.framework
- Ollama 0.40.15 on the same machine serves `/v1/responses`. With `SSIOS_OLLAMA_LIVE=1` and `SSIOS_VM_LIVE=1`, Codex in the guest called `exec_command` three times with `qwen2.5-coder:7b`. It wrote `marker.txt` in the workspace. Writing `/opt/ssios-outside` got a read-only filesystem, and connecting to `1.1.1.1:443` got `Operation not permitted`. The task reached `awaiting_approval` with `isolated` true and `boot_ms` 43363. The proxy log has only byte counts and SHA256. `deepseek-r1:7b` produced only a reasoning summary within 200 output tokens and made no tool call. `qwen2.5-coder:1.5b` produced invalid tool JSON. krunvm was not started
- Apple M4, macOS, krunvm 0.2.7: when the image was not cached, `scripts/macos-vm-smoke.sh` failed after about 31 seconds with `isolated` false and `error_code` `microvm_fail_closed`. After the user ran `krunvm create python:3.12-alpine`, the same smoke passed at `2166525`: `awaiting_approval`, `runtime_backend` `macos-vm`, `isolated` true, `boot_ms` 3513. No personal data. This Linux machine has no krunvm and did not reproduce that boot
- The local backend of Letta Code 0.34.45. The probe function is optional and does not run by default

## Planned

- A real microVM fast start and a clean single-use warm pool on Ubuntu
- An actual mount with one virtiofsd process per export
- A guest network broker. It is denied by default now, and there is no connector
- Snapshot restore. Identity and key reset have to come first. A snapshot that contains a vault, memory, or tokens is rejected now
- A measured boot chain and a TPM-sealed vault
- An optional confidential-computing / TEE profile
- Encryption at rest. The code only sets permission bits. It does not use LUKS or FileVault
- A semantic index from QMD or MemFS Search
- A graphical interface and messaging and procurement connectors
