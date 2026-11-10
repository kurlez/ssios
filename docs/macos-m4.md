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
3. Run the SSIOS dev profile in a Linux guest or in a local terminal. The dev profile does not need KVM
4. The production microVM profile needs `/dev/kvm` on Linux. macOS itself does not have that device. Whether it exists inside a guest has to be checked separately on the chosen virtualization product

The Ollama address in the SSIOS config must be a loopback address. The program rejects any other host name.

## A guest calling the host model

`127.0.0.1` inside the guest is the guest itself, not the Mac. Do not change Ollama to `0.0.0.0` by default.

If the guest really must call the host model, use a narrow forward. For example, forward only one port from the guest to the host's `127.0.0.1:11434`, and let the firewall accept only that guest address. After the forward is turned off, the dev profile's mock mode still works on its own. This repository does not ship a script that loosens the firewall or binds every NIC.

## If the demo reports git_failed after approval

Do not delete `.ssios` or `.ssios-demo`. Update, then run `make demo` again.

This `git rev-parse` failure was reproduced on Linux: the state directory was the relative path `.ssios-demo`, the repository directory itself existed, but `git -C` still used a relative path while the Git subprocess cwd was `.ssios-demo/git-home`, so it reported `cannot change to '.ssios-demo/memory/demo-alpha/repo'`. The fix resolves the state directory to an absolute path at startup. If the memory repository really does not exist and an approval still names an old revision, the program stops. It does not rebuild the repository and does not change the approval. That reproduction was not run again on a Mac.

## After the service is already up

An `ollama` process that can answer only shows that the local model process is listening. To finish one task, use the same state directory and config as the service that is already running. Do not start a second service, and do not delete `.ssios-memfs`.

Press Ctrl-C in the terminal that is running `ssios up`, update to the current branch, then start that one process again:

```bash
git fetch origin
git checkout cursor/ssios-local-prototype-6121
git pull
PYTHONPATH=src python3 -m ssios up --profile dev --state .ssios-memfs --config .ssios/memfs.toml
```

In another terminal:

```bash
PYTHONPATH=src python3 -m ssios ingest examples/notes \
  --project local-summary --state .ssios-memfs --config .ssios/memfs.toml
PYTHONPATH=src python3 -m ssios summary \
  --project local-summary --topic local-summary \
  --state .ssios-memfs --config .ssios/memfs.toml
```

`ingest` accepts only md and txt. It rejects symlinks and text that looks like a secret. Memory is written only after you type `yes`. `summary` reads only notes under `topics/local-summary/` and does not treat Letta's empty template as a body. A newly approved summary is still written to `topics/fixtures/last-summary.md` by a new git commit that replaces the file. It does not delete old history.

It proposes a summary only for the synthetic project `local-summary`. After reading the text that would be published and the memory diff, type `yes` to approve or `no` to deny. If input ends with no line, the approval stays pending. DeepSeek `message.thinking`, `<think>` content, and `approve` inside JSON do not become an approval.

These keys can be added under `[llm]` in `.ssios/memfs.toml`. Then restart the service with the original arguments. Do not delete the state directory, and do not pull the model again:

```toml
max_output_tokens = 1024
num_ctx = 8192
max_note_chars = 6000
summary_repair_retries = 1
```

A thinking model's summary asks for prose by default, not JSON. If it still fails, the output includes a diagnostic file path. The classification is:

```bash
PYTHONPATH=src python3 -m ssios diagnose <task_id> --state .ssios-memfs --config .ssios/memfs.toml
```

The raw output is in `.ssios-memfs/diagnostics/<task_id>.json`, mode 0600. It does not enter git and it is not auto-approved.

If the summary fails, the old task is kept. Truncated text, an empty answer, and text that restates internal fields do not reach approval. Run the same `ssios summary` again and it creates a new task. A bad summary already written to memory stays in git history. The next summary that receives `yes` replaces `topics/fixtures/last-summary.md` with a new commit. To separate the failure causes first, run:

```bash
PYTHONPATH=src python3 -m ssios ollama-probe --state .ssios-memfs --config .ssios/memfs.toml
```

This command uses the dev-profile control plane. It is not a microVM, and it does not treat "the model port is open" as a successful task.

`find .ssios-memfs -path '*topics/fixtures/last-summary.md'` showing several copies is because each read used to leave a Letta `memory export`. New code deletes that run's `exports/<agent>/scratch` after the read. Directories already left behind, named with four hex digits, are removed by the cleanup command. Look first, then delete. After stopping `ssios up`:

```bash
PYTHONPATH=src python3 -m ssios gc --dry-run --state .ssios-memfs --config .ssios/memfs.toml
PYTHONPATH=src python3 -m ssios gc --state .ssios-memfs --config .ssios/memfs.toml
```

The second command prints the list first and deletes only after `yes`. It does not delete Git history under `backend/memfs`.

The local page and the guest preflight:

```bash
brew install krunvm
krunvm --version
PYTHONPATH=src python3 -m ssios vm-doctor --state .ssios-memfs --config .ssios/memfs.toml
bash scripts/macos-vm-smoke.sh
PYTHONPATH=src python3 -m ssios ui-login --state .ssios-memfs --config .ssios/memfs.toml
```

`vm-doctor` only reports whether `krunvm` exists. `isolated` is still false: the repository has not actually started and destroyed a guest, and it has not measured cold-start time. After `backend` in the config is changed to `macos-vm`, the task fails closed and does not run on the Mac host instead. The page address is printed by `ui-login` and is valid only on `127.0.0.1`. You can also open `http://127.0.0.1:8765/ui/login` and paste `.ssios-memfs/secrets/human.token` once. Do not send the token to the repository or into chat.

## What this environment did not cover

- There is no macOS version number, so no virtualization product's compatibility matrix was verified
- Cloud Hypervisor was not started on Apple Silicon
- virtiofs was not mount-tested on the M4
- There is no automation of FileVault steps. Disk encryption remains a prerequisite in system settings
- `ssios summary` was not retested on this Mac. The matching tests on Linux use mock or an HTTP stub. They do not mean `deepseek-r1:7b` has been run here
