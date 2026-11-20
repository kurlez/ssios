# Backup and restore

`backup` writes one archive of the state directory. `restore` unpacks it into a new directory. Neither command deletes or overwrites anything.

```bash
./scripts/ssios stop
./scripts/ssios backup --out "$HOME/ssios-backups"
./scripts/ssios start
```

The archive is `ssios-backup-<UTC time>.tar.gz`, mode 0600, with a `manifest.json` that lists each file's size, mode, and SHA-256. The command prints the archive path and its own SHA-256.

`backup` refuses to run while the UI from `run/ui.pid` is alive, because a file could be copied half-written. `--allow-running` skips that check. The backup directory must be outside the state directory.

## What is in it

Everything under the state directory: accounts, password hashes, chat, memory repositories, vaults, Files libraries and their indexes, approvals, the audit log, the local tokens under `secrets/`, the UI TLS key, and the config when it is in the state directory.

Not in it:

- `run/` (pid and log), `cache/` (the Codex binary and kernel that `vm-prepare` fetches again), and `listen.url`.
- Anything in the OS keychain: Google tokens and the OAuth client secret. Reconnect Google after a restore.
- Symbolic links. They are listed under `skipped`.
- Ollama models.
- A library that keeps its files in its own folder outside the state directory (see `files-storage.md`). Back up that disk separately.

The archive is not encrypted. It contains password hashes, local tokens, and personal data. Keep it on an encrypted disk (FileVault, LUKS, or an encrypted external volume).

## Check an archive

```bash
./scripts/ssios backup-verify "$HOME/ssios-backups/ssios-backup-20261113T020000Z.tar.gz"
```

Every member is read and compared with the manifest. A missing file, an extra file, a changed byte, or a path outside the state directory fails with `backup_invalid`.

## Restore

```bash
./scripts/ssios restore "$HOME/ssios-backups/ssios-backup-20261113T020000Z.tar.gz" --into "$HOME/ssios-restored"
```

The destination must not exist, or must be an empty directory. The archive is verified completely before the first file is written. Directories are restored as 0700 and files keep their owner bits.

To use the restored copy, point `SSIOS_STATE` at it, or stop SSIOS, move the old state directory aside yourself, and move the restored one into its place. Then:

```bash
./scripts/ssios vm-prepare
./scripts/ssios start
./scripts/ssios audit-verify
```

Reconnect Google on the Connections page.

## Not done

There is no schedule, no rotation of old archives, and no incremental backup. Each run is a full copy. A launchd or cron entry that runs `backup --allow-running` is the operator's choice.
