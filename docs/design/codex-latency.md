# Codex model and latency

The guest default is `qwen3:14b` with `num_ctx` 16384. Tool turns send `think: false` on native `/api/chat`. `qwen2.5-coder:7b` and `qwen3-coder:30b` stay in `allow_models` and are not the default. Earlier tool-call notes for `qwen2.5-coder:7b` in `docs/status.md` are historical and are not a measurement of `qwen3:14b`.

`muse-glimmer:30b-mlx` is a candidate for Apple Silicon (Ollama MLX, about 19GB, Ollama >= 0.32.8, text and image, Apache 2.0, built for tool use). It is not the default. On a 24GB Mac set `[agent.codex] model` to that name and keep `num_ctx` at 16384. No Muse Glimmer timing or memory figure is recorded in this repository.

Other models that fit the same machine, and were not timed here:

| Model | Role | Note |
| --- | --- | --- |
| `qwen2.5:7b` | Host chat, Chinese and English | Already named for calendar sentences. Tool-call latency on an M4 is an estimate, not a measurement |
| `llama3.1:8b` | Alternate 8B chat model | Fits. No tool-call timing in this repository |
| `qwen2.5:14b` Q4 | Upgrade if a 7B plan fails | About 9GB. It would replace the 7B, not load beside it. The 40–90s planning figure in `codex-orchestrator.md` is an estimate |

A scripted guest turn on this Linux VM, with no model call, returns in under a second. That number is a harness measurement from 2026-11-11. It is not an M4 figure. The M4 table in `docs/design/codex-orchestrator.md` stays estimates.

One turn allows `step_limit` tool calls (default 6) and stops at `turn_timeout_seconds` (default 180). There is no cloud model fallback.

On 2026-11-11 a Mac retest showed the guest reach Booting and Running for a mail summary. Checks was still pending in that screenshot. No turn was timed, so this is not a latency measurement.

If a later turn fails, `./scripts/ssios vm-diagnose` prints a redacted exception line and the paths of `serial.log`, `proxy.log`, and the guest directory. It does not print tokens, cookies, or mail. The same files sit under `accounts/<id>/workspace/tasks/<task-id>/guest/`.
