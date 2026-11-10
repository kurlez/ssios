# macOS microVM

The guest on Apple Silicon uses `krunvm` (libkrun, Hypervisor.framework). The same command can also be used on Linux when KVM is available. This development machine is Linux and cannot start Hypervisor.framework. Cloud Hypervisor is the second path on Linux.

The guest runs `guest_worker.py`. It only reads the `grant.json` copied for this task, writes the prompt to `request.json` in scratch, waits for the host to write the model answer to `response.json`, then writes the body to `output.json`. The guest has no human token and no model address. The model call happens on the host and still uses only the configured mock or local Ollama. The quality check and human approval stay on the host. `isolated` is true only when the guest process actually exited and wrote `output.json`.

`krunvm create -v` has no read-only argument (the `krunvm-create` manual only has `HOST_PATH:GUEST_PATH`). The memory repository is therefore not mounted directly. The granted body is copied into this task's own directory and that copy is mounted. Changes the guest makes to the copy are not written back to memory. The local `virtiofsd` also has no read-only switch, so Cloud Hypervisor mounts a copied directory too.

krunvm has no argument that turns the guest network off, so the command line cannot prove that TSI is closed. The worker itself does not open a socket. The Cloud Hypervisor launch arguments contain no `--net`.

## Install

This repository does not ship krunvm. krunvm's own README currently says:

```bash
brew tap slp/krun
brew install krunvm
brew install buildah
krunvm --version
```

The Homebrew tap repository has also said `brew tap libkrun/krun` and `brew trust libkrun/krun`. Whichever one installs is the one your machine's `brew` accepts. After it is installed, run `krunvm --version` and use that output as the version. This file does not record a bottle hash that was not executed on a Mac.

`krunvm create` pulls an OCI image through buildah. The image is pinned to the manifest digest of `python:3.12-alpine` (read from `registry-1.docker.io` on 2026-11-09):

```text
docker.io/library/python@sha256:e064671ca452259bab50e8e2c7bd896e39e9a343bb06ca25234adf6fe18d0fc7
```

krunvm 0.2.7 has no environment variable for the storage volume. On the first run, if `storage_volume` in its own config is still empty, it asks for a mount point in the terminal. The default it suggests is `/Volumes/krunvm`, and it writes the answer to `~/Library/Application Support/rs.krunvm/krunvm.toml` (confy 0.4). Later `krunvm create` makes buildah use `root` and `runroot` under that volume. SSIOS `[runtime] macos_vm_volume` also defaults to `/Volumes/krunvm` and is used only by `vm-doctor` as a read-only check. If the volume is not mounted, the check says so, but it does not block a krunvm that can already run.

An external disk can use a case-sensitive sparse image. The paths below are examples. The script does not run these commands and does not call `hdiutil` or `diskutil`, which would change disks:

```bash
hdiutil create -size 60g -type SPARSE -fs "Case-sensitive APFS" -volname krunvm /path/to/external/krunvm.sparseimage
hdiutil attach /path/to/external/krunvm.sparseimage
diskutil info /Volumes/krunvm
```

When `volname` is `krunvm`, the mount point is `/Volumes/krunvm`. If krunvm has not remembered that path yet, run `krunvm list` once in the terminal and type that mount point when it asks.

The first pull of `python:3.12-alpine` can exceed the task timeout. Prepare the image before the smoke. `vm-prepare` runs `krunvm create` with the same image string the task uses. The timeout is `guest_prepare_timeout_seconds` (600 by default). It then deletes the virtual machine named `ssios-prepare`. The image stays in the buildah cache. The task's own create/start arguments do not change.

```bash
PYTHONPATH=src python3 -m ssios vm-prepare --state /tmp/ssios-vm-prepare --config /tmp/ssios-macos.toml
bash scripts/macos-vm-smoke.sh
```

On failure the smoke prints `error_detail`, the diagnostic file, and `up.log`, and it keeps the temporary state directory. It deletes that directory only when `SSIOS_SMOKE_CLEAN=1` is set.

## Config

In your own config (do not change the tracked sample unless you mean to), set the backend to:

```toml
[runtime]
backend = "macos-vm"
```

Then smoke it with a temporary state directory. It will not touch `.ssios`, `.ssios-demo`, or `.ssios-memfs`:

```bash
bash scripts/macos-vm-smoke.sh
PYTHONPATH=src python3 -m ssios vm-doctor --state /tmp/ssios-vm-doctor --config /tmp/ssios-vm-doctor-does-not-exist
```

`vm-doctor` needs a config with `backend = "macos-vm"`. It creates a guest, runs `echo ok` inside it, prints the elapsed time, and then deletes that virtual machine. Preflight itself does not boot a guest.

After `krunvm` is confirmed to work, stop the running service and start it again with the original state directory and the new `backend`. If startup fails, the task fails closed and does not run the summary on the Mac host instead.

This Linux machine has no krunvm. After `/dev/kvm` was given to the current user, Cloud Hypervisor v54.0 with the Linux 7.2.8 kernel measured: `echo ok` in 898 ms; the summary worker (virtio-fs, no `--net`, model mocked on the host) in 1576 ms, with `isolated` true. Those are not Mac numbers.

## Codex guest

`[runtime] agent = "codex"` uses Cloud Hypervisor on Linux and does not give the guest a NIC. krunvm on a Mac cannot turn the network off, so that path does not mark `isolated` true and the script does not claim it passed.

`scripts/macos-codex-smoke.sh` only downloads and checks the `rust-v0.167.0` aarch64 musl package on Apple Silicon, on the host (the hash is in `third_party/codex.pin.json`). It does not boot a virtual machine. The guest does not download again. The success path of that script has not been executed on this Linux machine.
