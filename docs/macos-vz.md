# A Codex guest on macOS with no NIC

`agent = "codex"` uses `macos-vz` on Apple Silicon and does not use krunvm. The summary worker still uses `macos-vm`. This note was checked against upstream docs on 2026-11-09. A Linux cloud environment cannot start Virtualization.framework, so this file does not contain a Mac boot result of its own.

## Why vfkit

| Option | No NIC | vsock | virtio-fs | Install | Why it was not chosen |
| --- | --- | --- | --- | --- | --- |
| vfkit 0.6.4 | Omitting `--device virtio-net` means there is no NIC. Devices are declared one by one | `--device virtio-vsock,port=…,socketURL=…,listen`. The host side is a Unix socket | `--device virtio-fs,sharedDir=…,mountTag=…` | `brew install vfkit`. Apache-2.0. The signed release has `com.apple.security.virtualization` | Chosen |
| Apple `container` 0.6.0 `--network none` | The example `ip addr` shows only `lo` | The docs do not expose vsock as a host socket we can attach | A container root filesystem, not the virtio-fs copy used here | `brew` is not necessarily the main path | Turning off the NIC does not connect the existing vsock proxy |
| A Swift helper written here | It can omit `VZVirtioNetworkDeviceConfiguration` | Possible | Possible | It would need its own signing and a maintained entitlement | Another binary to maintain |
| libkrun / krunvm 0.2.7 / krunkit | Without virtio-net, libkrun still opens TSI and the host forwards the socket | Present, but it is the TSI channel | krunvm `-v` | Already used for summaries | It cannot guarantee there is no outbound network |
| Lima `vz`, Tart | They can be configured with no NIC, but the default image and network stack are heavier | Present | Present | `brew` | Boot and images are thicker than vfkit and do not fit this read-only boundary |

vfkit usage: <https://github.com/crc-org/vfkit/blob/v0.6.4/doc/usage.md>. A direct boot needs an uncompressed arm64 `Image`. The kernel is pinned to cloud-hypervisor `ch-release-v7.2.8-20261009` `Image-arm64`, the same release as the x86_64 kernel already used on this Linux machine. Whether that arm64 build includes virtio-fs and vsock had to wait for the first boot on a Mac.

## Run it on an M4

Do not modify `.ssios`, `.ssios-demo`, or `.ssios-memfs`. The script uses a temporary directory and keeps it on failure. It does not create a disk volume.

```bash
brew install vfkit
ollama --version
ollama pull qwen2.5-coder:7b
bash scripts/macos-vz-codex-smoke.sh
```

The script changes the sample config to `backend = "macos-vz"`, `agent = "codex"`, and local Ollama. `vm-prepare` does not accept `--profile`. The profile comes from `profile = "dev"` in the config file. The household UI is `./scripts/ssios start` after `./scripts/ssios vm-prepare`, and `./scripts/ssios diagnose` checks vfkit without booting. This smoke script still starts a temporary `ssios up` only inside its own directory.

Ollama's OpenAI-compatible `/v1/responses` ignores `options` in the request, so the context stays at the 4096 default. Codex's system prompt plus tools is about 4098 tokens, so that path returns 400. The host proxy translates the same request to native `POST /api/chat` and sends `[agent.codex] num_ctx` (16384 by default, allowed from 8192 to 32768). The installed `qwen2.5-coder:7b` is not rewritten. The smoke script reads `context_length` from `/api/ps` before booting the guest. If it is still 4096, the script exits and prints these two recovery commands:

```bash
OLLAMA_CONTEXT_LENGTH=16384 ollama serve
printf 'FROM qwen2.5-coder:7b\nPARAMETER num_ctx 16384\n' > /tmp/ssios-Modelfile
ollama create ssios-qwen2.5-coder-7b-16k -f /tmp/ssios-Modelfile
```

The second command is needed only if `/api/ps` is still 4096 after the restart. Then set `[agent.codex] model` to `ssios-qwen2.5-coder-7b-16k` and add it to `allow_models`. A non-retryable upstream 4xx such as 400 stops vfkit immediately. The request cap stays at 4.

The guest root is not extracted onto the host disk. CPython's terminfo has 25 path pairs that differ only by case, for example `N/NCR260VT300WPP` pointing at `../n/ncr260vt300wpp`. On the default case-insensitive APFS that becomes a symlink to itself, and Python 3.14's `tarfile` filter then calls `realpath` and reports `Too many levels of symbolic links`. The initramfs is packed from tar members to cpio in memory. `share/terminfo` is not placed in the guest. Each smoke uses a new temporary directory. The directory left by the previous failure does not need to be deleted.

The host vsock socket is not placed in the state directory. macOS `sun_path` is only 104 bytes, and `/var/folders/...` is too long. The proxy listens on `/tmp/ssios-<random>` (0700), and the socket is 0600. vfkit 0.6.4 `listen` means the host listens and the guest connects to CID 2 port 19090. vfkit's REST socket is not opened. The serial log stays at `guest/serial.log` in the task directory. On failure its path and tail go into `diagnostics/<task>.json`. Homebrew vfkit already carries the `com.apple.security.virtualization` signature. This code does not sign it again.

The pinned `Image-arm64` (Linux 7.2.8) already contains virtio-vsock and virtio-fs. The M4 serial log contains `NET: Registered PF_VSOCK protocol family`. The failure point was that python-build-standalone CPython 3.12.16 musl was not built with `socket.AF_VSOCK`. The relay uses address family 40 and sends a 16-byte `sockaddr_vm` through musl `connect`. The guest self-checks within a few seconds of boot. If there is no vsock connection within 60 seconds, vfkit is stopped.

After Codex finishes, the guest runs one fixed probe in the same `workspace-write` sandbox: it writes `/workspace/marker.txt` and tries to write `/opt/ssios-outside`. That step does not call the model. The smoke passes only when the status is `awaiting_approval`, `isolated` is true, `marker` is true, `sandbox_denied` is true, `outside_write` is false, `nic` is only `lo`, and `outbound` is false. A missing marker is `probe_marker_missing`. An outside write that the sandbox did not deny is `probe_sandbox_not_denied`.
