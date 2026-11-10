# SSIOS

## The microVM cannot run tasks yet

The Cloud Hypervisor code only builds launch arguments and runs preflight. It has not booted a guest, and it is not a production execution environment. If KVM, `cloud-hypervisor`, `virtiofsd`, the kernel, or the disk is missing, it fails closed and does not run the task on the host instead. `dev-guard` is only an in-process tool allowlist.

## Two memory modes

The default `letta-memfs-compat` is this repository's own Git Markdown compatibility library. It lets the mock demo run when the Letta CLI is absent. It is not Letta MemFS.

The optional `letta-memfs-upstream` mode calls the pinned Letta Code `0.34.45` (Apache-2.0) local backend. It runs only the documented JSON subcommands: `agents create`, `memory tokens`, `memory export`, and `memory status`. It then makes an approved git commit in the `memfs/<agent-id>/memory` repository Letta created. It does not run `letta -p` and does not add `--yolo`. If the CLI is missing or the version does not match, it fails. It does not fall back to the compatibility library.

The control plane, approvals, audit log, and project storage stay on this machine. The dev profile binds only to `127.0.0.1` by default, and the model is the deterministic `mock`.

## Environment

- Python 3.11 or newer (this repository is tested on Python 3.12)
- Git 2.x (the MemFS compatibility library uses it for version commits)
- No third-party pip packages, cloud keys, Docker, or root

The dev profile binds only to `127.0.0.1` by default. The model provider is the deterministic `mock`. It does not use the network and does not download a model.

## Commands

```bash
make test      # regression tests, no keys required
make demo      # mock demo. Picks a free port and can run beside make up
make up        # start the dev profile in the foreground, 127.0.0.1:8765 by default, Ctrl-C to stop
make preflight # print the Cloud Hypervisor preflight. This does not boot a virtual machine
make memfs-info
```

`make demo` resolves `--state .ssios-demo` to an absolute path from the current working directory before handing it to Git. Internal Git can change its working directory to `git-home` without resolving a relative `-C` against the wrong place. It does not delete an existing `.ssios` or `.ssios-demo`. An expired approval stays expired. The original approval is not extended and its nonce is not reused. Running `make demo` again opens a new approval for the current revision and approves that new approval with the demo human token. A result that is already published with the same content is not published again. A task that was denied, cancelled, failed, or timed out is not retried in a loop by the same command. If that state directory should stay untouched, use a new directory and leave the old one as it is:

```bash
PYTHONPATH=src python3 -m ssios demo --state .ssios-demo-retry --config config/ssios.example.toml
```

Upstream MemFS smoke (the pinned version must already be installed, and this does not start an agent conversation):

```bash
npm install --prefix "$HOME/.ssios-letta" --no-fund --no-audit @letta-ai/letta-code@0.34.45
PYTHONPATH=src python3 -m ssios memfs-upstream-smoke \
  --state .ssios-upstream --letta-bin "$HOME/.ssios-letta/node_modules/.bin/letta"
```

The integrity check is in `third_party/letta-code.pin.json`. Node must satisfy that package's engines field. Do not put `LETTA_API_KEY` in the environment; this command drops that variable from the parent process.

You can also call the commands directly:

```bash
PYTHONPATH=src python3 -m ssios demo --state .ssios-demo --config config/ssios.example.toml
PYTHONPATH=src python3 -m ssios health --state .ssios --config config/ssios.example.toml
```

`make up` creates `.ssios/secrets/human.token` and `worker.token` on the first run, mode `0600`. Both files are gitignored. The human control token and the worker token are different. Only the human token can approve.

The demo submits a summary for the synthetic project `demo-alpha`, waits for human approval, writes memory, publishes, then restarts the process and reads the result back. The audit log records only the action and the payload hash.

## Connect local Ollama

mock and Ollama are two separate configs. There is no automatic fallback. Copy the sample and edit the copy. Do not write a real choice back into the tracked sample:

```bash
cp config/ssios.example.toml .ssios/ollama.toml
```

Change `provider` in `.ssios/ollama.toml` to `ollama`, and set `ollama_model` to the local model name. Start with a quantized 7B or 8B model. The name is configuration, not a hardcoded dependency. Ollama listens on `127.0.0.1:11434` by default. The first `ollama pull` downloads the model; inference can stay offline after that. This program does not pull models automatically and has no telemetry.

```bash
PYTHONPATH=src python3 -m ssios up --profile dev --state .ssios --config .ssios/ollama.toml
```

If the model is missing or the port is closed, the task fails with `llm_unavailable`. An Ollama process that can answer only shows that the process is listening. It does not mean the SSIOS task succeeded.

## Summarize against a service that is already running

`ssios summary` connects to a service that is already running. It does not start another process and it does not approve by itself. It runs only the synthetic project `local-summary` and does not change memory for `demo-alpha` or any other project. Use the same `--state` and `--config` as `ssios up`.

After updating the code, stop the old `ssios up` (do not delete the state directory), then start it once with the original arguments so the new code adds the `local-summary` export:

```bash
PYTHONPATH=src python3 -m ssios up --profile dev --state .ssios-memfs --config .ssios/memfs.toml
```

In another terminal, send the synthetic notes into memory. Read the diff, then type `yes`. This step does not bypass approval:

```bash
PYTHONPATH=src python3 -m ssios ingest examples/notes \
  --project local-summary --state .ssios-memfs --config .ssios/memfs.toml
PYTHONPATH=src python3 -m ssios summary \
  --project local-summary --topic local-summary \
  --state .ssios-memfs --config .ssios/memfs.toml
```

`summary` summarizes only note bodies under `topics/<topic>/`. The default templates in `human.md` and `persona.md` are not content. When there are no notes it returns `no_content` and does not write a restatement of the template into memory.

The command prints the summary it would publish and the memory diff first. `yes` approves, `no` denies, and ending input with no line leaves the approval pending. `approve`, `message.thinking`, and `<think>` in the model output are not treated as approval. The Ollama request includes a JSON schema. The answer is read only from `message.content`.

Failed tasks stay in the state directory. Truncated output, output that contains only thinking, and a summary that restates internal fields do not reach approval. Running `ssios summary` again creates a new task. If a bad summary was already approved, a new `yes` makes another git commit that replaces `topics/fixtures/last-summary.md`. The old commit remains. The state directory does not need to be deleted.

Thinking models receive `think: false`. The answer budget is `max_output_tokens` in the config (1024 by default). You can check whether the local Ollama accepts structured output before creating a task:

```bash
PYTHONPATH=src python3 -m ssios ollama-probe --state .ssios-memfs --config .ssios/memfs.toml
```

`dev-guard` finishes the task inside the control-plane process. `macos-vm` puts the summary worker in a guest after `krunvm` is installed; the model call is still proxied by the host. Without a guest the task fails closed. `isolated` is true only when the guest actually wrote the result. See [docs/macos-vm.md](docs/macos-vm.md).

Each Letta `memory export` leaves a full copy in the state directory. After the read, that run's `exports/<agent>/scratch` is deleted. Copies already accumulated under `exports/<agent>/<four hex digits>/` are not deleted automatically. Stop `ssios up` first, then list them:

```bash
PYTHONPATH=src python3 -m ssios gc --dry-run --state .ssios-memfs --config .ssios/memfs.toml
```

After the list contains only export copies, drop `--dry-run` and type `yes`. This command does not delete Git history under `backend/memfs`.

The local page listens only on `127.0.0.1`. The API still uses a Bearer token. Sign in from the browser first:

```bash
PYTHONPATH=src python3 -m ssios ui-login --state .ssios-memfs --config .ssios/memfs.toml
```

The printed address includes a one-time code that is valid for about two minutes. You can also open `http://127.0.0.1:8765/ui/login` and paste the human token. The token does not appear in the session file.

A Linux guest cannot reach `127.0.0.1` on the macOS host. Do not bind Ollama to `0.0.0.0` by default. If the guest must call the host model, forward only that guest address and restrict the source in the firewall. See [docs/macos-m4.md](docs/macos-m4.md).

## Docs

- [Architecture and trust boundary](docs/architecture.md)
- [MemFS integration boundary](docs/memfs.md)
- [Implementation status](docs/status.md)
- [Threat model](docs/security.md)
- [Mac mini M4 24GB](docs/macos-m4.md)
- [macOS guest preflight](docs/macos-vm.md)
- [Ubuntu and Cloud Hypervisor](docs/deployment-ubuntu.md)
- [Licenses](docs/licenses.md)
- Sample config: [config/ssios.example.toml](config/ssios.example.toml)

## License

Code in this repository uses the MIT license. The Letta MemFS concept and the Letta Code implementation belong to Letta. The upstream license is Apache-2.0. This repository does not copy Letta Code source.
