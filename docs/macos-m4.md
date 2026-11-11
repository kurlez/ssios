# Mac mini M4, 24GB unified memory

The target machine is an Apple M4 with 24GB of unified memory. The macOS version is unknown. Tests in this repository ran in a Linux x86_64 cloud environment. They were not run on that Mac, and model speed was not measured.

## Planning starting point

These numbers are only for leaving memory free. They are not a benchmark:

- Start the Linux guest at about 6GB of memory and 2 vCPUs, with the control plane and the memory repository inside it
- Keep only one Q4-quantized 7B or 8B model loaded in host Ollama at a time. Weight files of that class are commonly about 4–6GB. Real use grows as the context gets longer
- macOS, the guest, and the model still have to fit in 24GB. Do not start a second large model yet

Check the Lima, UTM, or Apple Virtualization docs from that time to see whether the guest product supports your macOS version. Do not assume the guest has usable nested KVM, and do not assume a GPU can be passed through to Linux.

## Recommended layout

1. Install Ollama on macOS and keep the default `127.0.0.1:11434`
2. Run `ollama pull` by hand when a model is needed. That step downloads data. Inference after that can stay off the network
3. Run `./scripts/ssios start` in a local terminal. See the README. The chat guest on this Mac is vfkit, not KVM
4. The production microVM profile needs `/dev/kvm` on Linux. macOS itself does not have that device. Whether it exists inside a guest has to be checked separately on the chosen virtualization product

The Ollama address in the SSIOS config must be a loopback address. The program rejects any other host name.

## A guest calling the host model

`127.0.0.1` inside the guest is the guest itself, not the Mac. Do not change Ollama to `0.0.0.0` by default.

If the guest really must call the host model, use a narrow forward. For example, forward only one port from the guest to the host's `127.0.0.1:11434`, and let the firewall accept only that guest address. After the forward is turned off, the dev profile's mock mode still works on its own. This repository does not ship a script that loosens the firewall or binds every NIC.

## If the demo reports git_failed after approval

Do not delete `.ssios` or `.ssios-demo`. Update, then run `make demo` again.

This `git rev-parse` failure was reproduced on Linux: the state directory was the relative path `.ssios-demo`, the repository directory itself existed, but `git -C` still used a relative path while the Git subprocess cwd was `.ssios-demo/git-home`, so it reported `cannot change to '.ssios-demo/memory/demo-alpha/repo'`. The fix resolves the state directory to an absolute path at startup. If the memory repository really does not exist and an approval still names an old revision, the program stops. It does not rebuild the repository and does not change the approval. That reproduction was not run again on a Mac.

## Household UI on this Mac

Start and stop with `./scripts/ssios`. The state directory is `$HOME/Library/Application Support/ssios`. Do not point `SSIOS_STATE` at `.ssios`, `.ssios-demo`, or `.ssios-memfs`, and do not delete those directories. An Ollama process that answers on `127.0.0.1:11434` only shows that the model is listening. Set `provider = "ollama"` in `$SSIOS_STATE/ollama.toml` and pull the chat model:

```bash
ollama pull qwen2.5:7b
./scripts/ssios restart
./scripts/ssios status
```

The chat guest is vfkit (`backend = "macos-vz"`, `agent = "codex"`), not krunvm. Install vfkit, prepare the guest files, then check prerequisites. This does not boot a guest:

```bash
brew install vfkit
./scripts/ssios vm-prepare
./scripts/ssios diagnose
```

`diagnose` reports vfkit, the kernel, the initramfs, the Codex binary, Ollama, and a keychain probe. A full boot that uses a temporary directory is `bash scripts/macos-vz-codex-smoke.sh`. See [macos-vz.md](macos-vz.md). The older krunvm summary guest is [macos-vm.md](macos-vm.md) and is not the chat route.

If this shell is SSH, unlock the login keychain before `./scripts/ssios start`:

```bash
security unlock-keychain ~/Library/Keychains/login.keychain-db
```

## What this environment did not cover

- There is no macOS version number, so no virtualization product's compatibility matrix was verified
- Cloud Hypervisor was not started on Apple Silicon
- virtiofs was not mount-tested on the M4
- There is no automation of FileVault steps. Disk encryption remains a prerequisite in system settings
- `ssios summary` was not retested on this Mac. The matching tests on Linux use mock or an HTTP stub. They do not mean `deepseek-r1:7b` has been run here
