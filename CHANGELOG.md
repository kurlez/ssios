# Changelog

## 0.1.0 — 2026-11-10

First prototype release of SSIOS, a local personal Agent OS. The package version is `0.1.0` (`pyproject.toml` and `ssios.__version__`).

- Letta MemFS-compatible Git Markdown memory (`letta-memfs-compat`), plus an optional pinned Letta Code 0.34.45 local backend (`letta-memfs-upstream`). Memory commits wait for human approval.
- Approval flow bound to the action, payload hash, expected revision, and a one-time nonce, with expiry and a loopback-only UI.
- Local models through Ollama, with no cloud fallback. The dev default stays the deterministic mock.
- Cloud Hypervisor microVM on Linux for the summary worker and for Codex, with no guest NIC.
- vfkit `macos-vz` Codex guest with no NIC. Commit `fe9cbd8` was verified on an Apple M4 Mac mini by `scripts/macos-vz-codex-smoke.sh`: `awaiting_approval`, `isolated` true, the workspace marker written, the sandbox denial recorded (`sandbox_denied` true), the outside write refused, and the only NIC `lo`.

### Known limitations

- No TEE, measured boot, or TPM-sealed vault. Disk encryption is an operator prerequisite (FileVault or LUKS), not a feature of this release.
- macOS is verified only for that `macos-vz` Codex smoke on the M4. The krunvm summary path was not re-checked for this tag.
- Gmail and Drive connectors are design notes only. There is no guest network broker.
- A dev-guard task is an in-process allowlist, not a virtual machine.
