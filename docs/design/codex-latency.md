# Codex model and latency

The guest default stays `qwen2.5-coder:7b` with `num_ctx` 16384. That is the only model this repository has recorded making a tool call, on Linux with Ollama 0.40.15 (`docs/status.md`). It fits a 24GB M4 beside a 2GB guest. `num_ctx` 32768 is allowed and is not the default: a 7B model at 32k does not leave room for the guest.

Other models that fit the same machine, and were not timed here:

| Model | Role | Note |
| --- | --- | --- |
| `qwen2.5:7b` | Host chat, Chinese and English | Already named for calendar sentences. Tool-call latency on an M4 is an estimate, not a measurement |
| `llama3.1:8b` | Alternate 8B chat model | Fits. No tool-call timing in this repository |
| `qwen2.5:14b` Q4 | Upgrade if a 7B plan fails | About 9GB. It would replace the 7B, not load beside it. The 40–90s planning figure in `codex-orchestrator.md` is an estimate |

A scripted guest turn on this Linux VM, with no model call, returns in under a second. That number is a harness measurement from 2026-11-11. It is not an M4 figure. The M4 table in `docs/design/codex-orchestrator.md` stays estimates.

One turn allows `step_limit` tool calls (default 6) and stops at `turn_timeout_seconds` (default 180). There is no cloud model fallback.
